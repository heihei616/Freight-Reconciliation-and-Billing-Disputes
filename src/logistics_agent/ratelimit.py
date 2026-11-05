"""请求限流、LLM 配额与并发控制。

为什么需要这一层
----------------
没有限流时，一个账号写个 for 循环就能打满模型额度、拖垮所有 SSE 连接。
这个模块补三件事：

1. **滑动窗口限流**：单位时间内每个用户最多发起 N 次提问。
2. **每日配额**：每个用户每天最多消耗 N 次模型调用，跨请求累计。
3. **并发闸门**：全局最多同时跑 M 个 Agent 执行循环，超出排队或拒绝。

一致性约定
----------
与 agent.py 的确认中断一样，这里的计数器是**单进程内存**。
多 worker 部署时必须换成 Redis（接口已按可替换的方式设计：
只需用同样的方法签名实现一个 RedisRateLimiter 即可）。
部署脚本已固定 --workers 1，不要擅自调大。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque


class RateLimitExceeded(Exception):
    """触发限流。retry_after 用于提示用户还要等多久。"""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.message = message
        self.retry_after = max(0, int(retry_after))


class QuotaExceeded(Exception):
    """当日配额用尽。与限流区分开，前端要给出不同的提示文案。"""

    def __init__(self, message: str, limit: int, used: int):
        super().__init__(message)
        self.message = message
        self.limit = limit
        self.used = used


@dataclass
class RateLimitConfig:
    """限流参数。默认值偏保守，适合内网小团队；可被环境变量覆盖。"""

    enabled: bool = True
    window_seconds: int = 60          # 滑动窗口长度
    max_requests_per_window: int = 10  # 窗口内每个用户最大提问次数
    daily_quota: int = 200            # 每用户每日模型调用上限
    max_concurrent_runs: int = 4      # 全局同时执行的 Agent 循环数
    acquire_timeout: float = 15.0     # 并发闸门的排队等待上限（秒）

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        """从环境变量读取，未设置的项用默认值。

        RATE_LIMIT_ENABLED=0 可整体关闭（仅建议本地调试用）。
        """
        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        enabled = os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() not in {
            "0", "false", "off", "no",
        }
        return cls(
            enabled=enabled,
            window_seconds=_int("RATE_LIMIT_WINDOW", 60),
            max_requests_per_window=_int("RATE_LIMIT_MAX_REQUESTS", 10),
            daily_quota=_int("LLM_DAILY_QUOTA", 200),
            max_concurrent_runs=_int("AGENT_MAX_CONCURRENT", 4),
            acquire_timeout=_float("AGENT_ACQUIRE_TIMEOUT", 15.0),
        )


@dataclass
class _Window:
    """单个用户的滑动窗口：只存时间戳，内存占用与请求数成正比。"""

    hits: Deque[float] = field(default_factory=deque)


class RateLimiter:
    """内存版限流器 + 配额计数 + 并发闸门。

    用法（FastAPI 依赖或直接进入口函数）：

        limiter = RateLimiter(RateLimitConfig.from_env())
        await limiter.check(user_id)          # 不通过就抛异常
        async with limiter.slot():            # 占一个并发位
            ...
        limiter.consume(user_id)              # 实际调用模型后记一次配额
    """

    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self._windows: dict[str, _Window] = defaultdict(_Window)
        self._daily: dict[tuple[str, str], int] = defaultdict(int)
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_runs)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        """按本地日期分桶，跨天自动归零，不需要额外的定时清理任务。"""
        return time.strftime("%Y-%m-%d", time.localtime())

    def _prune(self, window: _Window, now: float) -> None:
        """丢掉窗口之外的旧时间戳，防止长时间运行后队列无限增长。"""
        cutoff = now - self.config.window_seconds
        while window.hits and window.hits[0] <= cutoff:
            window.hits.popleft()

    # ------------------------------------------------------------------
    # 限流与配额
    # ------------------------------------------------------------------
    async def check(self, user_id: str) -> None:
        """检查是否允许发起一次提问。不通过则抛异常，不计数。"""
        if not self.config.enabled:
            return
        now = time.monotonic()
        async with self._lock:
            window = self._windows[user_id]
            self._prune(window, now)
            if len(window.hits) >= self.config.max_requests_per_window:
                # 还要等最旧那一次滑出窗口
                retry = self.config.window_seconds - (now - window.hits[0])
                raise RateLimitExceeded(
                    f"提问太频繁了，请 {max(1, int(retry))} 秒后再试",
                    retry_after=retry,
                )
            used = self._daily[(user_id, self._today())]
            if used >= self.config.daily_quota:
                raise QuotaExceeded(
                    f"今日问答次数已用完（{used}/{self.config.daily_quota}），明天再来",
                    limit=self.config.daily_quota,
                    used=used,
                )
            window.hits.append(now)

    def consume(self, user_id: str, amount: int = 1) -> None:
        """记录实际模型消耗。在真正发起 LLM 请求之后调用。"""
        if not self.config.enabled or amount <= 0:
            return
        self._daily[(user_id, self._today())] += amount

    def usage(self, user_id: str) -> dict[str, int]:
        """返回配额用量，给 /api/health 或前端提示用。"""
        used = self._daily[(user_id, self._today())]
        return {
            "daily_used": used,
            "daily_limit": self.config.daily_quota,
            "daily_remaining": max(0, self.config.daily_quota - used),
            "active_runs": self.config.max_concurrent_runs - self._semaphore._value,
        }

    # ------------------------------------------------------------------
    # 并发闸门
    # ------------------------------------------------------------------
    def slot(self) -> "_Slot":
        """获取一个并发执行位，配合 async with 使用。"""
        return _Slot(self._semaphore, self.config.acquire_timeout, self.config.enabled)

    # ------------------------------------------------------------------
    # 测试与运维
    # ------------------------------------------------------------------
    def apply_config(self, config: RateLimitConfig) -> None:
        """替换配置并同步重建信号量。

        只改 config 不改 semaphore 的话，并发上限会停留在旧值，
        这是一个容易漏掉的坑，所以收成一个方法。
        """
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_runs)

    def reset(self) -> None:
        """清空全部计数。只用于测试与本地调试，不要在接口里暴露。"""
        self._windows.clear()
        self._daily.clear()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_runs)

    def cleanup_idle(self, max_idle_seconds: int = 3600) -> int:
        """清掉长时间没有请求的窗口，返回清理条数。

        单进程长跑时，用户量大会让 _windows 缓慢膨胀；
        由应用层的周期任务调用即可，不需要额外的线程。
        """
        now = time.monotonic()
        stale = [
            uid for uid, window in self._windows.items()
            if not window.hits or now - window.hits[-1] > max_idle_seconds
        ]
        for uid in stale:
            self._windows.pop(uid, None)
        # 前一天的配额桶也顺手清掉
        today = self._today()
        old_keys = [key for key in self._daily if key[1] != today]
        for key in old_keys:
            self._daily.pop(key, None)
        return len(stale) + len(old_keys)


class _Slot:
    """异步上下文管理器：占住一个并发位，退出时必定归还。"""

    def __init__(self, semaphore: asyncio.Semaphore, timeout: float, enabled: bool):
        self._semaphore = semaphore
        self._timeout = timeout
        self._enabled = enabled
        self._acquired = False

    async def __aenter__(self) -> "_Slot":
        if not self._enabled:
            return self
        try:
            # 排队不能无限等：SSE 连接挂太久，用户会以为系统死了
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._timeout)
            self._acquired = True
        except asyncio.TimeoutError as exc:
            raise RateLimitExceeded(
                "当前排队人数较多，请稍后再试", retry_after=int(self._timeout),
            ) from exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._acquired:
            self._semaphore.release()
            self._acquired = False


# 应用级单例：app.py 在启动时覆盖为 from_env() 的配置
limiter = RateLimiter(RateLimitConfig())
