"""链路追踪：一个 trace_id 串起「一条日志 → 一次 SSE 流 → 一条 LangSmith trace」。

为什么需要它：
Agent 的 bug 几乎都不是崩溃，而是「这轮为什么选了错工具 / 为什么说查不到」。
没有 trace_id 时，只能靠时间戳在几百行日志里猜哪几行属于同一次对话。

设计取舍：
- **不装 LangSmith 也能用**。langsmith 是 langchain-core 的传递依赖，配了 Key 就自动上报；
  没配时 trace_id 仍然生成并打进日志与 SSE 事件，本地排查照样有效。
- trace_id 由我们自己生成（16 位 hex），不依赖上游 run id。
  上游的 run id 是树状的、一次对话有几十个，不适合当对外标识。
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4

from app.core.config import Settings

logger = logging.getLogger(__name__)

# 当前请求的 trace_id。空串表示不在追踪上下文里（如启动期、脚本直调）
current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="")


def new_trace_id() -> str:
    """生成一个新的 trace_id。

    取 16 位而非完整 uuid：日志里一行一个，短一点更好读；
    碰撞概率（1/2^64）在单机量级下可以忽略。
    """
    return uuid4().hex[:16]


def set_trace(trace_id: str) -> Token[str]:
    """进入追踪上下文，返回可用于还原的 token。"""
    return current_trace_id.set(trace_id)


def reset_trace(token: Token[str]) -> None:
    """退出追踪上下文。必须在 finally 里调用，否则会污染同一任务的后续请求。"""
    current_trace_id.reset(token)


def get_trace_id() -> str:
    return current_trace_id.get()


def configure_tracing(settings: Settings) -> bool:
    """按配置打开 LangSmith 上报，返回是否启用。

    LangChain 通过环境变量识别追踪开关，所以这里做的是「把配置写回环境」。
    用 setdefault 而非直接赋值：显式导出的环境变量优先级更高，便于临时覆盖。
    """
    if not settings.langsmith_tracing:
        return False
    if not settings.langsmith_api_key:
        logger.warning("已开启 LANGSMITH_TRACING 但缺少 LANGSMITH_API_KEY，追踪不会生效")
        return False

    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    if settings.langsmith_endpoint:
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
    # 旧版变量名，部分 langchain 版本仍只认这个
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")

    logger.info(
        "链路追踪已启用 | project=%s endpoint=%s",
        settings.langsmith_project,
        settings.langsmith_endpoint or "https://api.smith.langchain.com",
    )
    return True


def trace_config(trace_id: str, **metadata: Any) -> dict[str, Any]:
    """构造传给 astream_events 的 config。

    run_name 与 metadata 会一并出现在 LangSmith 的 trace 列表里，
    这样从面板上能直接按 session_id / user_id 过滤，不必先翻日志找 trace_id。
    """
    return {
        "run_name": f"chat-{trace_id}",
        "tags": ["chat"],
        "metadata": {"trace_id": trace_id, **{k: v for k, v in metadata.items() if v}},
    }
