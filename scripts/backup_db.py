"""
backup_db.py —— SQLite 在线备份、校验与恢复

为什么不用 copy 文件
-------------------
SQLite 默认是 WAL 模式，数据分散在 `chat.db`、`chat.db-wal` 里。
服务运行中直接 `cp chat.db`，拿到的可能是「缺了最近一段事务」的半成品，
而且没有任何报错——这种备份在真正需要时才发现不能用。

`sqlite3.Connection.backup()` 是 SQLite 官方的在线备份 API：
它在数据库层面复制一个一致性快照，允许源库同时被读写，不需要停服务。

用法
----
    # 备份（默认保留最近 14 份）
    PYTHONPATH=src python scripts/backup_db.py

    # 指定来源与保留份数
    PYTHONPATH=src python scripts/backup_db.py --source data/runtime/chat.db --keep 30

    # 只校验已有备份，不产生新文件
    PYTHONPATH=src python scripts/backup_db.py --verify data/runtime/backups/chat-20261105-221500.db

    # 恢复（服务必须已停止；会先把当前库另存一份再覆盖）
    PYTHONPATH=src python scripts/backup_db.py --restore <备份文件> --yes

退出码：0 成功，2 失败（校验不通过、文件不存在、服务未停）。
"""

from __future__ import annotations

import argparse
import os
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from logistics_agent.utils.paths import RUNTIME_DIR  # noqa: E402

# 核对用：这些表是业务主数据，备份后行数应完全一致。
# 不核对 agent_steps 这类日志表：备份期间仍在写入，行数允许有差异。
CORE_TABLES = ("users", "sessions", "messages", "validations", "drafts", "pending_confirms")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def _connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    # as_uri() 只接受绝对路径；命令行传相对路径是常态，这里统一收口。
    resolved = path.expanduser().resolve()
    if readonly:
        return sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    return sqlite3.connect(str(resolved))


def _finalize_single_file(conn: sqlite3.Connection) -> None:
    """
    把 WAL 合并回主文件并切回 DELETE 模式。

    backup() 会连 journal_mode 一起复制，目标库默认也是 WAL；
    这时 `chat-xxx.db` 旁边会多出 `-wal`，单独拷走备份就缺了最近事务。
    备份必须是「一个文件拿走就能用」。
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conn.execute("PRAGMA journal_mode=DELETE").fetchall()
    except sqlite3.Error:
        pass          # 已经关闭 WAL 或没有 WAL 时不必处理


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    existing = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in CORE_TABLES:
        if table in existing:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    return counts


def verify(path: Path) -> tuple[bool, str]:
    """完整性与可读性校验。返回 (是否通过, 说明)。"""
    if not path.exists():
        return False, f"文件不存在：{path}"
    try:
        conn = _connect(path, readonly=True)
    except sqlite3.Error as exc:
        return False, f"无法打开：{exc}"
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return False, f"integrity_check={integrity}"
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "sessions" not in tables:
            return False, "缺少 sessions 表，可能不是本项目的数据库"
        counts = _table_counts(conn)
    except sqlite3.Error as exc:
        return False, f"读取失败：{exc}"
    finally:
        conn.close()
    return True, f"完整，用户 {counts.get('users', 0)} · 会话 {counts.get('sessions', 0)} " \
                 f"· 消息 {counts.get('messages', 0)} · 草稿 {counts.get('drafts', 0)}"


def create_backup(source: Path, out_dir: Path, keep: int) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"源数据库不存在：{source}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"chat-{stamp}.db"
    if target.exists():                       # 同一秒内跑两次
        target = out_dir / f"chat-{stamp}-{datetime.now().microsecond:06d}.db"

    src = _connect(source, readonly=True)
    dst = _connect(target)
    try:
        src.backup(dst)                       # 一致性快照，允许源库继续被写
        _finalize_single_file(dst)
    finally:
        dst.close()
        src.close()

    ok, detail = verify(target)
    if not ok:
        target.unlink(missing_ok=True)        # 坏备份比没有备份更危险
        raise RuntimeError(f"备份校验失败：{detail}")

    # 行数核对：源库仍在被写，只比对核心表，且允许「源库更多」。
    src_conn = _connect(source, readonly=True)
    dst_conn = _connect(target, readonly=True)
    try:
        src_counts = _table_counts(src_conn)
        dst_counts = _table_counts(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    for table, count in dst_counts.items():
        if count > src_counts.get(table, 0):
            raise RuntimeError(f"{table} 行数异常：备份 {count} > 源库 {src_counts.get(table, 0)}")

    _prune(out_dir, keep)
    return target


def _prune(out_dir: Path, keep: int) -> list[Path]:
    """只清理本工具生成的 chat-*.db，绝不碰目录里的其他文件。"""
    if keep <= 0:
        return []
    files = sorted(out_dir.glob("chat-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in files[keep:]:
        old.unlink(missing_ok=True)
        removed.append(old)
    return removed


def _port_in_use(port: int = 8000) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def restore(source: Path, target: Path, assume_yes: bool) -> None:
    ok, detail = verify(source)
    if not ok:
        raise RuntimeError(f"备份不可用：{detail}")
    if _port_in_use():
        raise RuntimeError(
            "检测到 8000 端口仍在监听。恢复必须在服务停止后进行，"
            "否则内存里的连接会继续写入旧数据。请先停服再重试。")
    if not assume_yes:
        raise RuntimeError("恢复会覆盖当前数据库。确认后加 --yes 重新执行。")

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety = target.parent / "backups" / f"chat-before-restore-{stamp}.db"
        safety.parent.mkdir(parents=True, exist_ok=True)
        src = _connect(target, readonly=True)
        dst = _connect(safety)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        print(f"[恢复] 当前库已另存：{safety}")

    # 旧的 WAL/SHM 属于恢复前的库。必须先清掉再写入，
    # 否则 SQLite 会把残留的 WAL 回放到刚恢复的主文件上。
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
            print(f"[恢复] 已清理过期文件：{stale.name}")

    src = _connect(source, readonly=True)
    dst = _connect(target)
    try:
        src.backup(dst)                       # backup API 会覆盖目标内容
        _finalize_single_file(dst)
    finally:
        dst.close()
        src.close()

    ok, detail = verify(target)
    if not ok:
        raise RuntimeError(f"恢复后校验失败：{detail}")
    print(f"[恢复] 完成：{detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite 在线备份与恢复")
    parser.add_argument("--source", type=Path,
                        help="源数据库，默认取 LOGISTICS_DB_PATH 或 data/runtime/chat.db")
    parser.add_argument("--out-dir", type=Path, default=RUNTIME_DIR / "backups",
                        help="备份目录，默认 data/runtime/backups")
    parser.add_argument("--keep", type=int, default=14, help="保留最近 N 份，默认 14")
    parser.add_argument("--verify", type=Path, help="只校验指定备份，不新建")
    parser.add_argument("--restore", type=Path, help="从指定备份恢复")
    parser.add_argument("--yes", action="store_true", help="恢复时跳过确认提示")
    args = parser.parse_args()

    _load_dotenv()
    source = args.source or Path(
        os.getenv("LOGISTICS_DB_PATH", str(RUNTIME_DIR / "chat.db")))

    try:
        if args.verify:
            ok, detail = verify(args.verify)
            print(f"[校验] {'通过' if ok else '失败'}：{detail}")
            return 0 if ok else 2

        if args.restore:
            restore(args.restore, source, args.yes)
            return 0

        target = create_backup(source, args.out_dir, args.keep)
        size_mb = target.stat().st_size / 1024 / 1024
        print(f"[备份] 完成：{target}（{size_mb:.2f} MB）")
        ok, detail = verify(target)
        print(f"[校验] {'通过' if ok else '失败'}：{detail}")
        remaining = sorted(args.out_dir.glob("chat-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"[保留] 当前 {len(remaining)} 份（上限 {args.keep}）")
        return 0 if ok else 2
    except (RuntimeError, FileNotFoundError, OSError, sqlite3.Error) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
