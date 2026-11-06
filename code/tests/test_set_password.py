"""
初始凭证轮换脚本测试。

这组用例针对的是一个很容易被当成「写个 UPDATE 就完了」的操作：
口令哈希格式是 `pbkdf2_sha256$轮数$盐$哈希`，盐必须随机，
手写 SQL 改密几乎必然写出一个「看起来改了、实际登不上」的哈希。
所以测试重点不是脚本能不能跑，而是**改完之后登录逻辑认不认**。
"""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code/src"))

import scripts.set_password as sp  # noqa: E402
from logistics_agent import auth, db  # noqa: E402


def make_database(path: Path, *, username: str = "admin", password: str = "admin123") -> None:
    """用项目自己的建表逻辑造库，保证表结构与生产一致。"""
    original_path, original_conn = db.DB_PATH, db._conn
    db._conn = None
    db.DB_PATH = path
    try:
        asyncio.run(db.init_db())
        asyncio.run(db.create_user(username, auth.hash_password(password), "测试账号", "admin"))
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = original_conn
        db.DB_PATH = original_path


class PasswordPolicyTests(unittest.TestCase):
    def test_too_short_rejected(self):
        problems = sp.password_problems("Ab3!x")
        self.assertTrue(any("至少" in item for item in problems))

    def test_demo_password_rejected(self):
        # 换成演示口令等于没换，这是这个脚本存在的主要理由。
        for password in auth.DEMO_PASSWORDS.values():
            with self.subTest(password=password):
                problems = sp.password_problems(password)
                self.assertIn("不能与代码里写死的演示口令相同", problems)

    def test_weak_fragment_rejected(self):
        problems = sp.password_problems("MyPassword2026!")
        self.assertTrue(any("弱口令片段" in item for item in problems))

    def test_repeated_single_char_rejected(self):
        problems = sp.password_problems("a" * 16)
        self.assertTrue(any("同一个字符" in item for item in problems))

    def test_surrounding_space_rejected(self):
        # 首尾空格在页面上看不见，登录时必然输错，属于最常见的支持工单来源。
        problems = sp.password_problems(" CorrectHorseBattery9 ")
        self.assertTrue(any("空白" in item for item in problems))

    def test_overlong_rejected(self):
        self.assertTrue(sp.password_problems("x1" * 80))
        self.assertTrue(any("不超过" in item for item in sp.password_problems("x1" * 80)))

    def test_reasonable_password_passes(self):
        self.assertEqual(sp.password_problems("Qinghe-2026-Notebook"), [])


class RotateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        make_database(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _hash_of(self, username: str, path: Path | None = None) -> str:
        conn = sp._connect(path or self.db_path)
        try:
            return sp._find_user(conn, username)["password_hash"]
        finally:
            conn.close()

    def test_new_password_verifies_and_old_one_stops_working(self):
        new_password = "Qinghe-2026-Notebook"
        self.assertEqual(sp.cmd_set(self.db_path, "admin", new_password, quiet=True), 0)

        stored = self._hash_of("admin")
        self.assertTrue(auth.verify_password(new_password, stored))
        self.assertFalse(auth.verify_password("admin123", stored))

    def test_salt_changes_so_hash_differs_from_plain_rehash(self):
        # 同一口令两次哈希应不同（盐随机）。若相同说明盐被写死了。
        first = auth.hash_password("Qinghe-2026-Notebook")
        second = auth.hash_password("Qinghe-2026-Notebook")
        self.assertNotEqual(first, second)
        self.assertTrue(auth.verify_password("Qinghe-2026-Notebook", first))
        self.assertTrue(auth.verify_password("Qinghe-2026-Notebook", second))

    def test_weak_password_leaves_database_untouched(self):
        before = self._hash_of("admin")
        self.assertEqual(sp.cmd_set(self.db_path, "admin", "admin123"), 2)
        self.assertEqual(self._hash_of("admin"), before)

    def test_unknown_user_returns_failure(self):
        self.assertEqual(sp.cmd_set(self.db_path, "nobody", "Qinghe-2026-Notebook"), 2)

    def test_missing_database_returns_failure(self):
        missing = Path(self.tmp.name) / "absent.db"
        self.assertEqual(sp.cmd_set(missing, "admin", "Qinghe-2026-Notebook"), 2)
        self.assertEqual(sp.cmd_list(missing), 2)

    def test_list_marks_demo_password(self):
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            self.assertEqual(sp.cmd_list(self.db_path), 0)
        output = buffer.getvalue()
        self.assertIn("演示口令（必须更换）", output)
        self.assertIn("set_password.py --user admin", output)

    def test_list_marks_custom_password(self):
        sp.cmd_set(self.db_path, "admin", "Qinghe-2026-Notebook", quiet=True)
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            sp.cmd_list(self.db_path)
        self.assertIn("已自定义", buffer.getvalue())
        self.assertNotIn("必须更换", buffer.getvalue())


class ReadPasswordTests(unittest.TestCase):
    def _args(self, **kwargs):
        base = {"generate": False, "stdin": False, "password": None}
        base.update(kwargs)
        return type("Args", (), base)()

    def test_generated_password_passes_policy(self):
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            generated = sp._read_password(self._args(generate=True))
        self.assertIsNotNone(generated)
        self.assertEqual(sp.password_problems(generated), [])
        # 生成结果只在终端显示一次，不写进任何文件。
        self.assertIn(generated, buffer.getvalue())

    def test_stdin_reading(self):
        with patch("sys.stdin", io.StringIO("Qinghe-2026-Notebook\n")):
            self.assertEqual(sp._read_password(self._args(stdin=True)), "Qinghe-2026-Notebook")

    def test_empty_stdin_returns_none(self):
        with patch("sys.stdin", io.StringIO("\n")):
            self.assertIsNone(sp._read_password(self._args(stdin=True)))


class PathResolutionTests(unittest.TestCase):
    def test_env_override_wins(self):
        # 比较 Path 而不是字符串：Windows 会把 /tmp 归一成 \tmp，字符串断言会假失败。
        with patch.dict(os.environ, {"LOGISTICS_DB_PATH": "/tmp/custom.db"}):
            self.assertEqual(sp._db_path(), Path("/tmp/custom.db"))

    def test_default_is_runtime_dir(self):
        cleaned = {k: v for k, v in os.environ.items() if k != "LOGISTICS_DB_PATH"}
        with patch.dict(os.environ, cleaned, clear=True):
            self.assertEqual(sp._db_path().name, "chat.db")


if __name__ == "__main__":
    unittest.main()
