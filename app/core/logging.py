"""日志配置：给每条日志带上 trace_id。

uvicorn 默认只输出「级别 + 模块 + 消息」，一次对话产生的几十行日志混在一起，
排查时只能靠时间戳猜归属。这里给 root logger 挂一个 filter，
把当前上下文的 trace_id 注入 record，格式化成 `[abcd1234]` 前缀。

幂等：多次调用只配置一次，避免测试里重复挂 handler 导致日志翻倍。
"""

from __future__ import annotations

import logging
import sys

from app.core.tracing import get_trace_id

_CONFIGURED = False

# 没有 trace_id 时用短横线占位，保持列对齐，便于肉眼扫
_NO_TRACE = "-"

_FORMAT = "%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


class TraceIdFilter(logging.Filter):
    """把 contextvar 里的 trace_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or _NO_TRACE
        return True


def configure_logging(level: str = "INFO") -> None:
    """配置 root logger。重复调用无副作用。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(TraceIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper() or "INFO")

    # uvicorn 自带 handler，会绕过 root 的格式；这里把它的 handler 换成同一个
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True

    # 第三方库的 INFO 日志噪音太大（每次 HTTP 请求都打一行），只保留告警。
    # httpx2 是 langchain-openai 依赖的 httpx 分叉，logger 名不同，要单独压。
    for noisy in ("httpx", "httpcore", "httpx2", "langchain_openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
