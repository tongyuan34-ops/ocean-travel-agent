"""日志配置：给每条日志带上 trace_id。

uvicorn 默认只输出「级别 + 模块 + 消息」，一次对话产生的几十行日志混在一起，
排查时只能靠时间戳猜归属。这里给 root logger 挂一个 filter，
把当前上下文的 trace_id 注入 record，格式化成 `[abcd1234]` 前缀。

**打包成窗口程序后没有控制台**

PyInstaller 的 ``console=False`` 会把 ``sys.stdout`` / ``sys.stderr`` 置成
``None``。``StreamHandler(None)`` 每次写日志都抛 AttributeError，logging 内部
兜一层之后只会往（同样不存在的）stderr 打「--- Logging error ---」——
用户看到的现象是「双击没反应」，而我们这边一个字的线索都没有。

所以没有流的时候改成写文件：``logs/app.log``，滚动 3 份。桌面版用户报问题时，
让他把这个文件发过来就够了。

幂等：多次调用只配置一次，避免测试里重复挂 handler 导致日志翻倍。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

from app.core.paths import PROJECT_ROOT
from app.core.tracing import get_trace_id

_CONFIGURED = False

# 没有 trace_id 时用短横线占位，保持列对齐，便于肉眼扫
_NO_TRACE = "-"

_FORMAT = "%(asctime)s %(levelname)-7s [%(trace_id)s] %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

LOG_DIR = PROJECT_ROOT / "logs"
LOG_NAME = "app.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


class TraceIdFilter(logging.Filter):
    """把 contextvar 里的 trace_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or _NO_TRACE
        return True


def _build_handler() -> Any:
    """有控制台就打到控制台，没有就落到文件。

    判据是 ``sys.stdout`` 是否为 None 而不是 ``sys.frozen``：从任务计划、
    pythonw、某些 IDE 里跑起来也可能没有标准流，那时候同样需要文件日志。
    """
    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)
    stream = sys.stdout or sys.stderr
    if stream is None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            # utf-8-sig：中文 Windows 上记事本和 PowerShell 5.1 的 Get-Content
            # 默认按 ANSI(GBK) 读没有 BOM 的文件，中文日志会整段变乱码。
            # 用户把日志发过来时，第一句话往往是「这是乱码」——不如从源头免掉。
            handler: Any = RotatingFileHandler(
                LOG_DIR / LOG_NAME,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUPS,
                encoding="utf-8-sig",
            )
        except OSError:
            # 目录写不进去（装在 Program Files）时退回 NullHandler：
            # 宁可没有日志，也不能让每次写日志都抛异常
            return logging.NullHandler()
    else:
        handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    handler.addFilter(TraceIdFilter())
    return handler


def configure_logging(level: str = "INFO") -> None:
    """配置 root logger。重复调用无副作用。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_build_handler())
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


def reset() -> None:
    """把「已配置」的标记清掉。测试用——线上进程不需要重配日志。"""
    global _CONFIGURED
    _CONFIGURED = False
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
    root.handlers.clear()

