"""
preflight.py —— 上线前环境预检（把检查清单变成可执行的门禁）

为什么需要它
------------
`docs/prelaunch_checklist.md` 有 25 项人工验收，其中相当一部分其实是**环境事实**：
Python 版本够不够、数据库能不能写、磁盘还剩多少、演示口令改没改、备份有多旧。
这些靠人逐条敲命令既慢又容易漏，而漏掉任意一条的代价都是上线后才发现。

这个脚本一次性跑完，输出「通过 / 警告 / 失败」三档，退出码可直接用于发布流水线：
    0  没有失败项（有警告也会返回 0，除非加 --strict）
    2  存在失败项，不建议上线

用法
----
    PYTHONPATH=src python scripts/preflight.py
    PYTHONPATH=src python scripts/preflight.py --env production
    PYTHONPATH=src python scripts/preflight.py --strict --json

它只读不写：数据库用只读连接打开，写锁检查用 BEGIN IMMEDIATE 后立刻回滚，
不会留下任何数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from logistics_agent.utils.config import load_config, validate_env  # noqa: E402
from logistics_agent.utils.paths import PROJECT_ROOT, RUNTIME_DIR  # noqa: E402

EXPECTED_TABLES = {
    "sessions", "messages", "users", "settings", "tool_calls", "validations",
    "drafts", "agent_runs", "agent_steps", "prompt_revisions", "pending_confirms",
}

PLACEHOLDER_KEYS = {
    "replace-with-your-key", "your-key", "sk-xxx", "changeme", "",
}


@dataclass
class Check:
    name: str
    level: str                 # pass / warn / fail
    detail: str
    hint: str = ""

    @property
    def icon(self) -> str:
        return {"pass": "通过", "warn": "警告", "fail": "失败"}[self.level]


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name, level, detail, hint))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.level == "fail"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.level == "warn"]

    def as_dict(self) -> dict:
        return {
            "checks": [
                {"name": c.name, "level": c.level, "detail": c.detail, "hint": c.hint}
                for c in self.checks
            ],
            "failed": len(self.failed),
            "warned": len(self.warned),
            "passed": len(self.checks) - len(self.failed) - len(self.warned),
        }


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def check_python(report: Report) -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        report.add("Python 版本", "pass", f"{major}.{minor}.{sys.version_info.micro}")
    else:
        report.add("Python 版本", "fail", f"{major}.{minor} 低于要求",
                   "项目使用 3.11+ 语法（str | None、tomllib 等），请升级解释器")


def check_dependencies(report: Report) -> None:
    modules = [
        "fastapi", "uvicorn", "jinja2", "jwt", "httpx", "dotenv",
        "pydantic", "faiss", "numpy", "rank_bm25", "pypdf", "yaml",
    ]
    missing = []
    for name in modules:
        try:
            __import__(name)
        except Exception:       # noqa: BLE001 —— 依赖自身导入失败也算缺失
            missing.append(name)
    if missing:
        report.add("运行依赖", "fail", "缺少：" + "、".join(missing),
                   "pip install -r requirements.txt")
    else:
        report.add("运行依赖", "pass", f"{len(modules)} 个核心包均可导入")


def check_config(report: Report, config: dict) -> None:
    env_report = validate_env(config)
    if env_report.errors:
        report.add("运行时配置", "fail",
                   f"{len(env_report.errors)} 项错误：" +
                   "；".join(i.key for i in env_report.errors),
                   "运行 python -m logistics_agent.main 查看逐条提示")
    elif env_report.warnings:
        report.add("运行时配置", "warn",
                   f"{len(env_report.warnings)} 项警告：" +
                   "、".join(i.key for i in env_report.warnings),
                   "生产环境建议逐条确认")
    else:
        report.add("运行时配置", "pass", f"环境 {env_report.env}，无异常")


def check_storage(report: Report) -> None:
    """只读探测：预检不该顺手创建目录，否则「目录不存在」这个信号就消失了。"""
    targets = (
        ("运行数据目录", RUNTIME_DIR),
        ("知识库目录", PROJECT_ROOT / "data" / "knowledge"),
    )
    for label, path in targets:
        if not path.exists():
            report.add(label, "warn", f"{path} 不存在", "首次启动会自动创建")
            continue
        probe = path / ".preflight_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            report.add(label, "pass", str(path))
        except OSError as exc:
            report.add(label, "fail", f"{path} 不可写：{exc}",
                       "检查目录权限；容器部署时确认卷已挂载且用户可写")


def check_database(report: Report, db_path: Path) -> None:
    if not db_path.exists():
        report.add("数据库", "warn", f"{db_path} 尚不存在",
                   "首次启动会自动建表；上线前先启动一次再跑预检")
        return

    try:
        # as_uri() 会给出 file:///C:/... 或 file:///home/...，
        # 手工拼 f"file:{path}" 在 Windows 上会被当成相对路径。
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        report.add("数据库", "fail", f"无法打开：{exc}", "确认文件未被占用、路径正确")
        return

    if integrity == "ok":
        size_mb = db_path.stat().st_size / 1024 / 1024
        report.add("数据库完整性", "pass", f"integrity_check=ok，{size_mb:.1f} MB")
    else:
        report.add("数据库完整性", "fail", integrity[:200],
                   "立即从最近备份恢复，不要继续写入")

    missing = EXPECTED_TABLES - tables
    if missing:
        report.add("数据表", "warn", "缺少：" + "、".join(sorted(missing)),
                   "启动一次服务会自动建表；若仍缺失，检查是否连错了库")
    else:
        report.add("数据表", "pass", f"{len(EXPECTED_TABLES)} 张业务表齐全")

    # 写锁检查：能拿到再立刻回滚，不留下任何数据
    try:
        probe = sqlite3.connect(str(db_path), timeout=1.0)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            report.add("数据库可写", "pass", "能获取写锁")
        finally:
            probe.close()
    except sqlite3.Error as exc:
        report.add("数据库可写", "warn", f"暂时拿不到写锁：{exc}",
                   "服务运行中时属正常；若已停止仍失败，检查文件权限或磁盘只读")


def check_demo_accounts(report: Report, db_path: Path) -> None:
    if not db_path.exists():
        return
    try:
        from logistics_agent import auth, db

        original_path, original_conn = db.DB_PATH, db._conn
        db._conn = None
        db.DB_PATH = db_path
        try:
            risky = asyncio.run(auth.insecure_demo_accounts())
        finally:
            if db._conn is not None:
                db._conn.close()
            db._conn = original_conn
            db.DB_PATH = original_path
    except Exception as exc:      # noqa: BLE001 —— 预检不该因单项异常整体失败
        report.add("演示口令", "warn", f"检查失败：{exc}", "手工确认账号口令已修改")
        return

    if risky:
        report.add("演示口令", "fail", "仍在使用：" + "、".join(risky),
                   "逐个更换：python scripts/set_password.py --user <账号>；生产环境会直接拒绝启动")
    else:
        report.add("演示口令", "pass", "未发现使用默认口令的账号")


def check_bootstrap(report: Report, db_path: Path) -> None:
    """
    空库 + 生产环境 + 没设引导口令 = 起不来。

    生产环境不再播种演示账号，所以一个全新的生产库必须靠
    ADMIN_INITIAL_PASSWORD 建出第一个管理员。这个组合如果留到启动时才暴露，
    表现是「容器起来就退出」，日志里只有一句 RuntimeError；放在预检里能提前说清。
    """
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        try:
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        report.add("首次引导", "warn", f"读取 users 表失败：{exc}",
                   "确认库文件正确；空库首次启动会自动建表")
        return

    if total:
        report.add("首次引导", "pass", f"已有 {total} 个账号，无需引导")
        return
    if os.getenv("APP_ENV", "development").strip().lower() == "production" \
            and not (os.getenv("ADMIN_INITIAL_PASSWORD") or "").strip():
        report.add("首次引导", "fail", "生产库为空且未设置 ADMIN_INITIAL_PASSWORD",
                   "设置至少 12 位的 ADMIN_INITIAL_PASSWORD 后首次启动，"
                   "登录后立即改密并从环境变量中移除")
    else:
        report.add("首次引导", "warn", "用户表为空",
                   "开发环境首次启动会创建演示账号；生产环境需 ADMIN_INITIAL_PASSWORD")


def check_secrets(report: Report) -> None:
    llm_key = (os.getenv("LLM_API_KEY") or "").strip()
    provider = (os.getenv("LLM_PROVIDER") or "openai").lower()
    if provider == "mock":
        report.add("模型密钥", "warn", "当前为 mock 模式，不会调用真实模型",
                   "验收环境请接入真实模型")
    elif llm_key.lower() in PLACEHOLDER_KEYS or len(llm_key) < 12:
        report.add("模型密钥", "fail", "未配置或仍是占位值",
                   "填写真实密钥；密钥只放环境变量，不要提交到仓库")
    else:
        report.add("模型密钥", "pass", f"已配置（{len(llm_key)} 字符，未打印内容）")

    secret = (os.getenv("JWT_SECRET") or "").strip()
    if len(secret) >= 32:
        report.add("JWT 密钥", "pass", f"显式配置，{len(secret)} 字符")
    elif secret:
        report.add("JWT 密钥", "warn", f"仅 {len(secret)} 字符，强度不足",
                   "建议至少 32 字符：python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
    else:
        report.add("JWT 密钥", "warn", "未显式配置，将使用主目录密钥文件",
                   "多实例或容器部署必须显式设置，否则重启后全员掉线")


def check_disk(report: Report) -> None:
    usage = shutil.disk_usage(PROJECT_ROOT)
    free_gb = usage.free / 1024 ** 3
    if free_gb < 0.2:
        report.add("磁盘空间", "fail", f"仅剩 {free_gb:.2f} GB",
                   "SQLite 写入需要空间；清理或扩容后再上线")
    elif free_gb < 1:
        report.add("磁盘空间", "warn", f"仅剩 {free_gb:.2f} GB",
                   "日志与备份会持续增长，建议保留 1 GB 以上")
    else:
        report.add("磁盘空间", "pass", f"剩余 {free_gb:.1f} GB")


def check_port(report: Report, config: dict) -> None:
    port = int(config.get("server", {}).get("port", 8000))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        in_use = sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()
    if in_use:
        report.add("服务端口", "warn", f"127.0.0.1:{port} 已被占用",
                   "若服务已在运行属正常；否则先停止旧进程再启动")
    else:
        report.add("服务端口", "pass", f"127.0.0.1:{port} 空闲")


def check_backup(report: Report) -> None:
    backup_dir = RUNTIME_DIR / "backups"
    files = sorted(backup_dir.glob("chat-*.db"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if backup_dir.exists() else []
    if not files:
        report.add("数据库备份", "warn", "没有找到任何备份",
                   "运行 python scripts/backup_db.py 生成第一份基线备份")
        return
    age_days = (time.time() - files[0].stat().st_mtime) / 86400
    if age_days > 7:
        report.add("数据库备份", "warn", f"最近一份备份已是 {age_days:.1f} 天前",
                   "建议每日备份，并定期做恢复演练")
    else:
        report.add("数据库备份", "pass",
                   f"最近备份 {age_days:.1f} 天前，共 {len(files)} 份")


def check_env_file_permissions(report: Report) -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        report.add(".env 文件", "warn", "不存在，将只读取系统环境变量",
                   "本地开发可复制 .env.example；生产用编排注入环境变量")
        return
    if os.name != "posix":
        report.add(".env 文件", "pass", "存在（Windows 不做 POSIX 权限检查）")
        return
    mode = env_file.stat().st_mode & 0o777
    if mode & 0o077:
        report.add(".env 文件", "warn", f"权限过宽：{oct(mode)}",
                   "chmod 600 .env —— 同机其他用户不应读到密钥")
    else:
        report.add(".env 文件", "pass", f"权限 {oct(mode)}")


def run(config: dict) -> Report:
    report = Report()
    check_python(report)
    check_dependencies(report)
    check_config(report, config)
    check_storage(report)

    db_path = Path(os.getenv("LOGISTICS_DB_PATH", str(RUNTIME_DIR / "chat.db"))).expanduser()
    check_database(report, db_path)
    check_bootstrap(report, db_path)
    check_demo_accounts(report, db_path)
    check_secrets(report)
    check_disk(report)
    check_port(report, config)
    check_backup(report)
    check_env_file_permissions(report)
    return report


def print_text(report: Report, env: str) -> None:
    print("=" * 64)
    print(f"上线预检（环境：{env}）")
    print("=" * 64)
    for check in report.checks:
        print(f"[{check.icon}] {check.name}：{check.detail}")
        if check.hint and check.level != "pass":
            print(f"        → {check.hint}")
    passed = len(report.checks) - len(report.failed) - len(report.warned)
    print("-" * 64)
    print(f"通过 {passed} 项 · 警告 {len(report.warned)} 项 · 失败 {len(report.failed)} 项")
    if report.failed:
        print("结论：存在失败项，不建议上线。")
    elif report.warned:
        print("结论：可以上线，但请先确认警告项。")
    else:
        print("结论：全部通过。")


def main() -> int:
    parser = argparse.ArgumentParser(description="上线前环境预检")
    parser.add_argument("--env", choices=["development", "production"],
                        help="覆盖 APP_ENV，用于按生产标准检查")
    parser.add_argument("--strict", action="store_true", help="把警告也视为失败")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于流水线解析")
    args = parser.parse_args()

    _load_dotenv()
    if args.env:
        os.environ["APP_ENV"] = args.env

    config = load_config()
    report = run(config)

    if args.json:
        print(json.dumps({"env": os.getenv("APP_ENV", "development"),
                          **report.as_dict()}, ensure_ascii=False, indent=2))
    else:
        print_text(report, os.getenv("APP_ENV", "development"))

    if report.failed:
        return 2
    if args.strict and report.warned:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
