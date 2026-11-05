"""
登录防爆破测试。

这组用例针对的是一个「看不见的功能」：没有它，系统照样能登录，
只是攻击者可以无限重试；而 PBKDF2 的 20 万次迭代会把每次重试变成
一小段 CPU 时间——也就是说，**没有限流的强哈希等于给自己加了一个 CPU 放大器**。

因此这里既验「锁得住」，也验「锁得准」：正常用户手误两次不能被牵连，
换 IP 或换用户名也不能绕过。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent.ratelimit import (  # noqa: E402
    LoginGuard,
    LoginGuardConfig,
    RateLimitExceeded,
)


def guard(**kwargs) -> LoginGuard:
    base = dict(enabled=True, max_failures=3, window_seconds=900,
                ip_max_failures=5, ip_window_seconds=300)
    base.update(kwargs)
    return LoginGuard(LoginGuardConfig(**base))


class AccountLimitTests(unittest.TestCase):
    def test_locks_after_configured_failures(self):
        g = guard(max_failures=3)
        for _ in range(2):
            g.check("1.1.1.1", "admin")
            g.record_failure("1.1.1.1", "admin")
        # 第三次之前仍然放行：阈值是「已失败次数 >= 上限」才拒
        g.check("10.0.0.9", "admin")   # 换 IP 也拦，因为是账号维度超限
        g.record_failure("1.1.1.1", "admin")
        with self.assertRaises(RateLimitExceeded) as ctx:
            g.check("10.0.0.9", "admin")
        self.assertIn("分钟", ctx.exception.message)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_other_accounts_unaffected(self):
        g = guard(max_failures=2)
        g.record_failure("1.1.1.1", "admin")
        g.record_failure("1.1.1.1", "admin")
        with self.assertRaises(RateLimitExceeded):
            g.check("1.1.1.1", "admin")
        # 同一 IP 上的另一个账号还在 IP 阈值内，应该放行
        g.check("1.1.1.1", "employee")

    def test_success_clears_account_counter(self):
        g = guard(max_failures=2)
        g.record_failure("1.1.1.1", "admin")
        g.record_failure("1.1.1.1", "admin")
        g.record_success("1.1.1.1", "admin")
        # 用户想起来密码了，不应该继续被自己之前的失败挡住
        g.check("1.1.1.1", "admin")


class IpLimitTests(unittest.TestCase):
    def test_spraying_many_usernames_hits_ip_limit(self):
        """撞库的典型形态：一个 IP、一堆用户名。账号维度不会触发，必须靠 IP。"""
        g = guard(max_failures=10, ip_max_failures=3)
        for i in range(3):
            g.check("2.2.2.2", f"user{i}")
            g.record_failure("2.2.2.2", f"user{i}")
        with self.assertRaises(RateLimitExceeded) as ctx:
            g.check("2.2.2.2", "another")
        self.assertIn("频繁", ctx.exception.message)

    def test_success_does_not_reset_ip_counter(self):
        """
        关键取舍：成功登录只清账号计数，不清 IP。

        否则攻击者只要有一个自己的合法账号，登录一次就能重置 IP 桶，
        撒网攻击永远触发不了 IP 阈值。
        """
        g = guard(max_failures=100, ip_max_failures=2)
        g.record_failure("3.3.3.3", "a")
        g.record_failure("3.3.3.3", "b")
        g.record_success("3.3.3.3", "a")
        with self.assertRaises(RateLimitExceeded):
            g.check("3.3.3.3", "c")

    def test_other_ips_unaffected(self):
        g = guard(max_failures=100, ip_max_failures=1)
        g.record_failure("4.4.4.4", "a")
        with self.assertRaises(RateLimitExceeded):
            g.check("4.4.4.4", "b")
        g.check("5.5.5.5", "b")


class WindowTests(unittest.TestCase):
    def test_expired_failures_no_longer_count(self):
        g = guard(max_failures=1, window_seconds=1, ip_max_failures=99)
        g.record_failure("6.6.6.6", "admin")
        with self.assertRaises(RateLimitExceeded):
            g.check("6.6.6.6", "admin")
        # 直接把时间戳往前挪，避免真的 sleep 一秒
        hits = g._users["admin"]
        hits[0] -= 2
        g.check("6.6.6.6", "admin")

    def test_disabled_guard_allows_everything(self):
        g = guard(enabled=False, max_failures=1)
        for _ in range(5):
            g.record_failure("7.7.7.7", "admin")
            g.check("7.7.7.7", "admin")


class MemoryTests(unittest.TestCase):
    def test_sweep_removes_expired_keys(self):
        """随机用户名攻击不能让字典无限增长。"""
        g = guard(max_failures=1000, window_seconds=1, ip_max_failures=1000)
        for i in range(1200):
            g.record_failure(f"10.0.{i // 250}.{i % 250}", f"u{i}")
        self.assertGreater(len(g._users), 0)
        # 把全部时间戳挪到窗口外，再触发一次 sweep
        for hits in g._users.values():
            for i in range(len(hits)):
                hits[i] -= 5
        for hits in g._ips.values():
            for i in range(len(hits)):
                hits[i] -= 500
        g.record_failure("9.9.9.9", "fresh")
        self.assertLess(len(g._users), 1200)

    def test_reset_clears_both_dimensions(self):
        g = guard(max_failures=1)
        g.record_failure("8.8.8.8", "admin")
        g.reset()
        g.check("8.8.8.8", "admin")


if __name__ == "__main__":
    unittest.main()
