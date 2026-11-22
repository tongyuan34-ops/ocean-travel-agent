"""进程内指标：HTTP、对话轮次、工具调用、技能命中。

为什么自己写而不是引入 ``prometheus_client``：

- 依赖表已经很长，而这里需要的只有「计数器 + 直方图 + 文本输出」三件事；
- 单进程桌面版根本没人抓取，多一个依赖只会拖慢启动；
- 实现足够小（两百行以内），行为可测，出问题不用去读第三方库。

**多 worker 的边界**：每个进程各算各的，``uvicorn --workers 4`` 时
``/metrics`` 只反映收到这次请求的那个 worker。要聚合就交给抓取端
（Prometheus 按实例分别抓），或者用单 worker + Redis 存状态。
这一点写清楚，比假装「总数是准的」好。

**不要把用户数据放进标签**：标签进入时间序列，一旦出现会话 ID、问题文本，
基数会爆炸。当前只允许方法、路径模板、状态码、工具名、技能 key 这类枚举值。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# 直方图分桶（秒）。按「网页请求 / 一轮对话 / 一次工具调用」三种量级分别定，
# 共用一套会让对话轮次全落在最后几个桶里，分位数就失去了分辨力
_BUCKETS: dict[str, tuple[float, ...]] = {
    "http_request_duration_seconds": (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2.5,
        5,
        10,
        30,
        60,
    ),
    "chat_turn_duration_seconds": (1, 2.5, 5, 10, 20, 30, 45, 60, 90, 120, 300),
    "tool_call_duration_seconds": (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
}
_DEFAULT_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 300)
# 分位数用最近 N 个样本精确算。再往前只保留分桶计数：内存有界，
# 而运营看板上「最近两千次」已经足够定位问题
_SAMPLE_WINDOW = 2048

Labels = tuple[tuple[str, str], ...]


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    count: int = 0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=_SAMPLE_WINDOW))

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        self.total += value
        self.count += 1
        self.samples.append(value)
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1
                return
        # 超过最大桶的样本仍然计入 _sum / _count，只是不出现在 _bucket 里。
        # Prometheus 的 +Inf 桶因此必须单独给，否则总量对不上

    def percentile(self, q: float) -> float | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return ordered[index]


class Metrics:
    """线程安全的指标注册表。

    单进程里既有 asyncio 任务也有 ``_spawn`` 出去的线程（偏好抽取），
    所有写入都走同一把锁。粒度很粗，但每次操作只有一次字典写入，
    不是瓶颈；换成细粒度锁会让代码复杂十倍。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, Labels], float] = {}
        self._histograms: dict[tuple[str, Labels], _Histogram] = {}
        self._started_at = time.time()

    @staticmethod
    def _labels(labels: dict[str, Any]) -> Labels:
        return tuple(sorted((str(key), str(value)) for key, value in labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = (name, self._labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = (name, self._labels(labels))
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = _Histogram(_BUCKETS.get(name, _DEFAULT_BUCKETS))
                self._histograms[key] = histogram
            histogram.observe(value)

    def reset(self) -> None:
        """测试用：清空数据但不重置启动时间。"""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    def snapshot(self) -> dict[str, Any]:
        """JSON 快照。结构按指标名分组，前端/脚本不需要认识具体业务。"""
        with self._lock:
            counters = dict(self._counters)
            histograms = dict(self._histograms.items())

        series: dict[str, list[dict[str, Any]]] = {}
        for (name, labels), value in counters.items():
            series.setdefault(name, []).append({"labels": dict(labels), "value": round(value, 6)})
        for (name, labels), histogram in histograms.items():
            series.setdefault(name, []).append(
                {
                    "labels": dict(labels),
                    "count": histogram.count,
                    "sum_seconds": round(histogram.total, 6),
                    "p50_ms": _ms(histogram.percentile(0.50)),
                    "p95_ms": _ms(histogram.percentile(0.95)),
                    "p99_ms": _ms(histogram.percentile(0.99)),
                }
            )
        return {
            "uptime_seconds": round(time.time() - self._started_at, 3),
            "series": series,
        }

    def prometheus(self) -> str:
        """Prometheus 文本格式（version 0.0.4）。"""
        with self._lock:
            counters = dict(self._counters)
            histograms = dict(self._histograms.items())

        lines: list[str] = []
        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{_fmt_labels(labels)} {value:g}")
        for (name, labels), histogram in sorted(histograms.items()):
            cumulative = 0
            for edge, count in zip(histogram.buckets, histogram.counts, strict=True):
                cumulative += count
                bucket_labels = (*labels, ("le", f"{edge:g}"))
                lines.append(f"{name}_bucket{_fmt_labels(bucket_labels)} {cumulative}")
            lines.append(f"{name}_bucket{_fmt_labels((*labels, ('le', '+Inf')))} {histogram.count}")
            lines.append(f"{name}_sum{_fmt_labels(labels)} {histogram.total:g}")
            lines.append(f"{name}_count{_fmt_labels(labels)} {histogram.count}")
        return "\n".join(lines) + ("\n" if lines else "")


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


def _fmt_labels(labels: Labels) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return "{" + body + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


metrics = Metrics()

# 指标端点自己不进统计，否则抓取频率会污染 HTTP 分位数
SKIP_PATHS = frozenset({"/metrics", "/api/v1/metrics"})


class MetricsMiddleware:
    """纯 ASGI 中间件：记录请求数、状态码与耗时。

    不用 ``BaseHTTPMiddleware``：它会把响应体收齐再发，SSE 会从逐字输出
    退化成「等十几秒、整段突然出现」。这和 ``app/standalone.py`` 里
    鉴权中间件选择纯 ASGI 是同一个理由。
    """

    def __init__(self, app: Any, skip: frozenset[str] = SKIP_PATHS) -> None:
        self.app = app
        self._skip = skip

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if path in self._skip:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET")
        started = time.perf_counter()
        status = 500

        async def _send(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status") or 500)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            # 路由模板在 finally 里才一定可用；直接用原始 path 会让
            # /sessions/abc123 这种每次都产生新时间序列
            route = scope.get("route")
            template = str(getattr(route, "path", "") or "unmatched")
            elapsed = time.perf_counter() - started
            metrics.inc("http_requests_total", method=method, path=template, status=str(status))
            metrics.observe("http_request_duration_seconds", elapsed, method=method, path=template)


__all__ = ["SKIP_PATHS", "Metrics", "MetricsMiddleware", "metrics"]
