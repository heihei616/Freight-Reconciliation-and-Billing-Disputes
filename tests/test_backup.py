"""
备份与恢复测试。

备份工具最容易出的问题不是「没备份」，而是「备份了但用不了」：
半截的 WAL、损坏的文件、恢复时旧 WAL 把数据又盖回去。
这组用例用临时库把这些路径都跑一遍，不碰真实数据。
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import scripts.backup_db as backup  # noqa: E402
from logistics_agent import db  # noqa: E402


def make_database(path: Path, *, username: str = "tester", message: str = "你好") -> None:
    """用项目自己的建表逻辑造一个真实结构的库。"""
    original_path, original_conn = db.DB_PATH, db._conn
    db._conn = None
    db.DB_PATH = path
    try:
        asyncio.run(db.init_db())
        user = asyncio.run(db.create_user(username, "hash", "测试员", "employee"))
        session = asyncio.run(db.create_session(user["id"], "备份测试"))
        asyncio.run(db.add_message(session["id"], "user", message))
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = original_conn
        db.DB_PATH = original_path


def count(path: Path, table: str) -> int:
    import sqlite3
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    finally:
        conn.close()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "chat.db"
        self.out = self.dir / "backups"
        make_database(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_is_verified_and_complete(self):
        target = backup.create_backup(self.source, self.out, keep=5)
        self.assertTrue(target.exists())
        ok, detail = backup.verify(target)
        self.assertTrue(ok, detail)
        self.assertEqual(count(target, "users"), count(self.source, "users"))
        self.assertEqual(count(target, "messages"), count(self.source, "messages"))

    def test_backup_file_is_standalone(self):
        """备份必须是单文件完整库，不能依赖源库的 -wal。"""
        target = backup.create_backup(self.source, self.out, keep=5)
        self.assertFalse(Path(str(target) + "-wal").exists())
        self.assertTrue(backup.verify(target)[0])

    def test_prune_keeps_latest(self):
        now = time.time()
        for index in range(5):
            fake = self.out / f"chat-2026010{index + 1}-000000.db"
            fake.parent.mkdir(parents=True, exist_ok=True)
            fake.write_bytes(b"x")
            os.utime(fake, (now - 100 + index, now - 100 + index))
        backup._prune(self.out, keep=2)
        remaining = sorted(p.name for p in self.out.glob("chat-*.db"))
        self.assertEqual(remaining, ["chat-20260104-000000.db", "chat-20260105-000000.db"])

    def test_verify_rejects_garbage(self):
        broken = self.dir / "broken.db"
        broken.write_bytes(b"this is not a sqlite database" * 10)
        ok, detail = backup.verify(broken)
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_verify_rejects_foreign_database(self):
        import sqlite3
        foreign = self.dir / "foreign.db"
        conn = sqlite3.connect(str(foreign))
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        ok, detail = backup.verify(foreign)
        self.assertFalse(ok)
        self.assertIn("sessions", detail)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source = self.dir / "chat.db"
        self.target = self.dir / "target.db"
        make_database(self.source, username="source_user", message="源库消息")
        make_database(self.target, username="target_user", message="目标库消息")

    def tearDown(self):
        self.tmp.cleanup()

    def test_restore_replaces_target_content(self):
        with patch.object(backup, "_port_in_use", return_value=False):
            backup.restore(self.source, self.target, assume_yes=True)
        self.assertEqual(count(self.target, "users"), count(self.source, "users"))
        self.assertEqual(count(self.target, "messages"), count(self.source, "messages"))
        self.assertTrue(backup.verify(self.target)[0])

    def test_restore_requires_explicit_yes(self):
        with patch.object(backup, "_port_in_use", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                backup.restore(self.source, self.target, assume_yes=False)
        self.assertIn("--yes", str(ctx.exception))

    def test_restore_refuses_while_service_running(self):
        with patch.object(backup, "_port_in_use", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                backup.restore(self.source, self.target, assume_yes=True)
        self.assertIn("停止", str(ctx.exception))

    def test_restore_keeps_a_copy_of_current_database(self):
        with patch.object(backup, "_port_in_use", return_value=False):
            backup.restore(self.source, self.target, assume_yes=True)
        safety = list((self.dir / "backups").glob("chat-before-restore-*.db"))
        self.assertEqual(len(safety), 1)
        # 另存的那份应该是恢复前的目标库，用户是 target_user
        rows = count(safety[0], "users")
        self.assertEqual(rows, 1)


if __name__ == "__main__":
    unittest.main()
