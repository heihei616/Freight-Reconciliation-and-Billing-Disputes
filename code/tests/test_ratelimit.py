"""限流、配额与并发闸门测试，全部离线运行。"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "code/src"))

from logistics_agent import ratelimit


def make_limiter(**overrides) -> ratelimit.RateLimiter:
    config = ratelimit.RateLimitConfig(
        enabled=True,
        window_seconds=60,
        max_requests_per_window=3,
        daily_quota=5,
        max_concurrent_runs=2,
        acquire_timeout=0.2,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return ratelimit.RateLimiter(config)


class SlidingWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_within_limit_pass(self):
        limiter = make_limiter()
        for _ in range(3):
            await limiter.check("u1")  # 不抛异常即通过

    async def test_fourth_request_is_rejected(self):
        limiter = make_limiter()
        for _ in range(3):
            await limiter.check("u1")
        with self.assertRaises(ratelimit.RateLimitExceeded) as ctx:
            await limiter.check("u1")
        self.assertGreater(ctx.exception.retry_after, 0)

    async def test_users_are_isolated(self):
        limiter = make_limiter()
        for _ in range(3):
            await limiter.check("u1")
        await limiter.check("u2")  # 另一个用户不受影响

    async def test_disabled_limiter_never_blocks(self):
        limiter = make_limiter(enabled=False)
        for _ in range(20):
            await limiter.check("u1")


class DailyQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_exhaustion_raises(self):
        limiter = make_limiter(max_requests_per_window=100, daily_quota=2)
        limiter.consume("u1", 2)
        with self.assertRaises(ratelimit.QuotaExceeded) as ctx:
            await limiter.check("u1")
        self.assertEqual(ctx.exception.limit, 2)
        self.assertEqual(ctx.exception.used, 2)

    async def test_usage_report(self):
        limiter = make_limiter()
        limiter.consume("u1", 2)
        usage = limiter.usage("u1")
        self.assertEqual(usage["daily_used"], 2)
        self.assertEqual(usage["daily_remaining"], 3)

    async def test_zero_or_negative_consume_ignored(self):
        limiter = make_limiter()
        limiter.consume("u1", 0)
        limiter.consume("u1", -3)
        self.assertEqual(limiter.usage("u1")["daily_used"], 0)

    async def test_cleanup_removes_previous_day_buckets(self):
        limiter = make_limiter()
        limiter._daily[("u1", "2020-01-01")] = 9
        removed = limiter.cleanup_idle()
        self.assertGreaterEqual(removed, 1)
        self.assertNotIn(("u1", "2020-01-01"), limiter._daily)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slots_are_released(self):
        limiter = make_limiter()
        for _ in range(5):  # 超过并发上限也能依次完成，说明锁被正确归还
            async with limiter.slot():
                await asyncio.sleep(0)
        self.assertEqual(limiter.usage("u1")["active_runs"], 0)

    async def test_slot_timeout_raises(self):
        limiter = make_limiter(max_concurrent_runs=1, acquire_timeout=0.05)
        async with limiter.slot():
            with self.assertRaises(ratelimit.RateLimitExceeded):
                async with limiter.slot():
                    pass

    async def test_slot_released_on_exception(self):
        limiter = make_limiter(max_concurrent_runs=1)
        with self.assertRaises(RuntimeError):
            async with limiter.slot():
                raise RuntimeError("模拟执行失败")
        async with limiter.slot():  # 若没释放，这里会超时
            pass

    async def test_concurrent_tasks_respect_limit(self):
        limiter = make_limiter(max_concurrent_runs=2, acquire_timeout=2)
        running = 0
        peak = 0

        async def worker():
            nonlocal running, peak
            async with limiter.slot():
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.02)
                running -= 1

        await asyncio.gather(*[worker() for _ in range(6)])
        self.assertLessEqual(peak, 2)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = ratelimit.RateLimitConfig()
        self.assertTrue(config.enabled)
        self.assertGreater(config.daily_quota, 0)

    def test_apply_config_rebuilds_semaphore(self):
        limiter = make_limiter(max_concurrent_runs=2)
        limiter.apply_config(ratelimit.RateLimitConfig(max_concurrent_runs=7))
        self.assertEqual(limiter._semaphore._value, 7)

    def test_reset_clears_state(self):
        limiter = make_limiter()
        limiter.consume("u1", 3)
        limiter.reset()
        self.assertEqual(limiter.usage("u1")["daily_used"], 0)


if __name__ == "__main__":
    unittest.main()
