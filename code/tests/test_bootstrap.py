"""
首次启动的账号引导。

这组用例锁的是一个真实存在过的死锁：
旧实现无条件播种 `admin/admin123`，紧接着又因为「仍在使用演示口令」拒绝启动，
于是全新的生产库永远起不来，而报错里建议的「登录后台改密码」也进不去。

修法是把「播种演示账号」和「生产引导」分开：生产环境不播种子，
第一次启动靠 `ADMIN_INITIAL_PASSWORD` 建出唯一的管理员。
"""

import asyncio
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "code/src"))

import scripts.preflight as preflight  # noqa: E402
from logistics_agent import auth, db  # noqa: E402

GOOD_INITIAL = "Qinghe-2026-Notebook"


@contextmanager
def env(**values):
    """只改动与本组用例相关的三个变量，其余环境保持原样。"""
    keys = ("APP_ENV", "SEED_DEMO_USERS", "ADMIN_INITIAL_PASSWORD")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        for key, value in values.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TempDbCase(unittest.TestCase):
    """每个用例一个空库；db.py 是懒加载单例，必须同时重置 _conn。"""

    def setUp(self):
        self._orig_path, self._orig_conn = db.DB_PATH, db._conn
        db._conn = None
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "bootstrap.db"
        db.DB_PATH = self.db_path
        asyncio.run(db.init_db())

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
        db._conn = self._orig_conn
        db.DB_PATH = self._orig_path
        self._tmp.cleanup()

    def count_users(self) -> int:
        return asyncio.run(db.count_users())

    def user(self, username: str):
        return asyncio.run(db.get_user_by_username(username))


class SeedingSwitchTests(unittest.TestCase):
    def test_development_seeds_by_default(self):
        with env(APP_ENV="development"):
            self.assertTrue(auth.demo_seeding_enabled())

    def test_production_does_not_seed_by_default(self):
        with env(APP_ENV="production"):
            self.assertFalse(auth.demo_seeding_enabled())

    def test_explicit_flag_wins(self):
        with env(APP_ENV="production", SEED_DEMO_USERS="1"):
            self.assertTrue(auth.demo_seeding_enabled())
        with env(APP_ENV="development", SEED_DEMO_USERS="0"):
            self.assertFalse(auth.demo_seeding_enabled())


class EnsureAccountsTests(TempDbCase):
    def test_development_creates_demo_accounts(self):
        with env(APP_ENV="development"):
            created = asyncio.run(auth.ensure_initial_accounts())
        self.assertEqual(created, ["admin", "employee"])
        self.assertEqual(self.count_users(), 2)
        self.assertTrue(auth.verify_password("admin123", self.user("admin")["password_hash"]))

    def test_production_empty_db_refuses_to_start_without_bootstrap(self):
        with env(APP_ENV="production"):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(auth.ensure_initial_accounts())
        message = str(ctx.exception)
        # 报错必须是「可执行的下一步」，不是一句「配置错误」
        self.assertIn("ADMIN_INITIAL_PASSWORD", message)
        self.assertIn("set_password.py", message)
        self.assertEqual(self.count_users(), 0)

    def test_production_bootstraps_admin_from_env(self):
        with env(APP_ENV="production", ADMIN_INITIAL_PASSWORD=GOOD_INITIAL):
            created = asyncio.run(auth.ensure_initial_accounts())
        self.assertEqual(created, [])
        self.assertEqual(self.count_users(), 1)
        admin = self.user("admin")
        self.assertEqual(admin["role"], auth.ROLE_ADMIN)
        # 关键是「建出来的哈希能被登录逻辑认出来」，而不是只看行数
        self.assertTrue(auth.verify_password(GOOD_INITIAL, admin["password_hash"]))
        self.assertIsNone(self.user("employee"))

    def test_weak_initial_password_is_refused(self):
        with env(APP_ENV="production", ADMIN_INITIAL_PASSWORD="admin123"):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(auth.ensure_initial_accounts())
        self.assertIn("不合规", str(ctx.exception))
        self.assertEqual(self.count_users(), 0)

    def test_existing_accounts_are_never_overwritten(self):
        asyncio.run(db.create_user(
            "admin", auth.hash_password(GOOD_INITIAL), "系统管理员", auth.ROLE_ADMIN))
        with env(APP_ENV="development", SEED_DEMO_USERS="1"):
            created = asyncio.run(auth.ensure_initial_accounts())
        self.assertEqual(created, [])
        self.assertEqual(self.count_users(), 1)
        # 已自定义的口令不能被启动流程悄悄改回演示口令
        self.assertTrue(auth.verify_password(GOOD_INITIAL, self.user("admin")["password_hash"]))
        self.assertFalse(auth.verify_password("admin123", self.user("admin")["password_hash"]))

    def test_leftover_bootstrap_variable_is_ignored_with_notice(self):
        asyncio.run(db.create_user(
            "admin", auth.hash_password(GOOD_INITIAL), "系统管理员", auth.ROLE_ADMIN))
        with env(APP_ENV="production", ADMIN_INITIAL_PASSWORD=GOOD_INITIAL):
            with patch("builtins.print") as fake_print:
                asyncio.run(auth.ensure_initial_accounts())
        printed = " ".join(str(arg) for call in fake_print.call_args_list for arg in call.args)
        self.assertIn("ADMIN_INITIAL_PASSWORD", printed)
        self.assertNotIn(GOOD_INITIAL, printed)      # 口令本身不许进日志


class DemoHintTests(TempDbCase):
    """登录页上的演示账号提示只该在「确实还能用」时出现。"""

    def _create(self, username, password, display, role):
        asyncio.run(db.create_user(username, auth.hash_password(password), display, role))

    def test_lists_only_accounts_still_on_demo_password(self):
        self._create("admin", "admin123", "系统管理员", auth.ROLE_ADMIN)
        self._create("employee", GOOD_INITIAL, "张员工", auth.ROLE_EMPLOYEE)
        with env(APP_ENV="development"):
            hints = asyncio.run(auth.demo_account_hints())
        self.assertEqual([h["username"] for h in hints], ["admin"])
        self.assertEqual(hints[0]["password"], "admin123")

    def test_disappears_after_rotation(self):
        self._create("admin", GOOD_INITIAL, "系统管理员", auth.ROLE_ADMIN)
        self._create("employee", GOOD_INITIAL, "张员工", auth.ROLE_EMPLOYEE)
        with env(APP_ENV="development"):
            self.assertEqual(asyncio.run(auth.demo_account_hints()), [])

    def test_hidden_in_production_even_if_still_demo(self):
        self._create("admin", "admin123", "系统管理员", auth.ROLE_ADMIN)
        with env(APP_ENV="production"):
            self.assertEqual(asyncio.run(auth.demo_account_hints()), [])


class PreflightBootstrapTests(TempDbCase):
    def test_empty_production_db_fails_before_deploy(self):
        report = preflight.Report()
        with env(APP_ENV="production"):
            preflight.check_bootstrap(report, self.db_path)
        self.assertEqual(len(report.failed), 1)
        self.assertIn("ADMIN_INITIAL_PASSWORD", report.failed[0].hint)

    def test_empty_production_db_passes_with_bootstrap_password(self):
        report = preflight.Report()
        with env(APP_ENV="production", ADMIN_INITIAL_PASSWORD=GOOD_INITIAL):
            preflight.check_bootstrap(report, self.db_path)
        self.assertEqual(report.failed, [])

    def test_non_empty_db_needs_no_bootstrap(self):
        asyncio.run(db.create_user(
            "admin", auth.hash_password(GOOD_INITIAL), "系统管理员", auth.ROLE_ADMIN))
        report = preflight.Report()
        with env(APP_ENV="production"):
            preflight.check_bootstrap(report, self.db_path)
        self.assertEqual(report.failed, [])
        self.assertEqual(report.warned, [])


if __name__ == "__main__":
    unittest.main()
