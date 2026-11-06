"""
进程内指标（Prometheus 文本格式），零外部依赖。

为什么不用 prometheus_client
---------------------------
只需要六个指标，引入一个依赖会让「装完 requirements 就能跑」变重，
而文本 exposition 格式本身就是几十行的事。代价是要自己保证：

* **标签基数可控**：只按路由模板（`/api/sessions/{session_id}`）而不是真实路径统计，
  否则每个会话 ID 都会生成一条时间序列，抓取端先崩；
* **并发安全**：请求计数在事件循环里累加，SSE 活跃数可能跨线程读，统一加锁；
* **输出稳定**：按 key 排序，抓取结果可直接 diff。

指标刻意保持「少而能定位问题」：请求量/错误率看 `http_requests_total`，
卡不卡看 `sse_active` 与 `http_request_seconds`，业务堵点看 `pending_confirms`。
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any


def _escape(value: str) -> str:
    """Prometheus 标签值转义：反斜杠、双引号、换行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str, str]] = Counter()
        self._duration_sum: dict[tuple[str, str], float] = {}
        self._duration_count: Counter[tuple[str, str]] = Counter()
        self._sse_active = 0
        self._started_at = time.time()

    # -- 写入 --------------------------------------------------------------

    def observe_request(self, method: str, route: str, status: int,
                        duration_sec: float) -> None:
        key = (method.upper(), route or "unmatched", str(status))
        with self._lock:
            self._requests[key] += 1
            dkey = (method.upper(), route or "unmatched")
            self._duration_sum[dkey] = self._duration_sum.get(dkey, 0.0) + duration_sec
            self._duration_count[dkey] += 1

    def sse_started(self) -> None:
        with self._lock:
            self._sse_active += 1

    def sse_finished(self) -> None:
        with self._lock:
            self._sse_active = max(0, self._sse_active - 1)

    # -- 渲染 --------------------------------------------------------------

    @property
    def uptime_sec(self) -> float:
        return time.time() - self._started_at

    def render(self, *, version: str, extra: dict[str, Any] | None = None) -> str:
        """
        输出 Prometheus 文本格式。

        `extra` 放需要现查的 gauge（待确认数、库大小、切片数），
        由调用方提供，避免指标模块反过来依赖 db / rag。
        """
        with self._lock:
            requests = dict(self._requests)
            duration_sum = dict(self._duration_sum)
            duration_count = dict(self._duration_count)
            sse_active = self._sse_active

        lines = [
            "# HELP logistics_build_info 构建信息，固定为 1",
            "# TYPE logistics_build_info gauge",
            f'logistics_build_info{{version="{_escape(version)}"}} 1',
            "# HELP logistics_uptime_seconds 进程启动至今的秒数",
            "# TYPE logistics_uptime_seconds gauge",
            f"logistics_uptime_seconds {self.uptime_sec:.1f}",
            "# HELP logistics_sse_active 当前活跃的 SSE 流数量",
            "# TYPE logistics_sse_active gauge",
            f"logistics_sse_active {sse_active}",
        ]

        if requests:
            lines += [
                "# HELP logistics_http_requests_total 按方法/路由模板/状态码统计的请求数",
                "# TYPE logistics_http_requests_total counter",
            ]
            for (method, route, status), count in sorted(requests.items()):
                lines.append(
                    f'logistics_http_requests_total{{method="{_escape(method)}",'
                    f'route="{_escape(route)}",status="{_escape(status)}"}} {count}'
                )

        if duration_count:
            lines += [
                "# HELP logistics_http_request_seconds 请求耗时（秒）",
                "# TYPE logistics_http_request_seconds summary",
            ]
            for (method, route), count in sorted(duration_count.items()):
                total = duration_sum.get((method, route), 0.0)
                labels = f'method="{_escape(method)}",route="{_escape(route)}"'
                lines.append(f"logistics_http_request_seconds_sum{{{labels}}} {total:.4f}")
                lines.append(f"logistics_http_request_seconds_count{{{labels}}} {count}")

        for name, value in sorted((extra or {}).items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        return "\n".join(lines) + "\n"


# 进程级单例：中间件与 SSE 生成器共用同一份计数
metrics = Metrics()
