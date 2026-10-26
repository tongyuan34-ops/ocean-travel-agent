"""工具调用韧性：失败分类、退避重试、重试计数。

Agent 应用里「工具挂了」是常态而不是异常：地图 API 抖动、搜索被限流、
采集超时。原来的实现把这些一律当成「返回错误文本给模型」，模型只能
自己重试或编造，行为不可控。

这里补的是**代码层的确定性重试**，但只对真正可恢复的失败生效。

最关键的一条约束：**权限拒绝永远不能重试。**

`require_permission` 是 fail-closed 的，用户拒绝（或 120 秒未确认）意味着
「不要执行」。如果重试逻辑把它当成瞬时故障重试三次，等于给了模型三次
绕过用户确认的机会——那套权限网关就白写了。所以分类函数里
`PermissionDenied` 排在最前面，优先于任何「看起来像网络错误」的文本匹配。

同理，`asyncio.CancelledError` 必须原样向上抛：用户点了停止，
任何吞掉它的重试都会让「停止」失效。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.tools import BaseTool

from app.agent.permissions import PermissionDenied
from app.core.security import PathNotAllowed
from app.media.base import MediaError

logger = logging.getLogger(__name__)

# 包装标记：graph 每次重建都会重新包一遍工具，靠它避免套娃
_RESILIENT_MARK = "_resilient_wrapped"

# 这些异常代表「同样的请求再发一次也没用」：要么是代码问题，要么是用户意图
NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PermissionDenied,
    PathNotAllowed,
    ValueError,
    TypeError,
    KeyError,
    NotImplementedError,
)

# 媒体层错误按 code 区分：上游抖动可重试，没配后端重试多少次都一样
RETRYABLE_MEDIA_CODES = frozenset({"MEDIA_UPSTREAM_TIMEOUT", "MEDIA_UPSTREAM_ERROR"})

# 自定义适配器（采集、检索）抛的裸异常只能靠文本判断，作为最后兜底
_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "rate limit",
    "too many requests",
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)

# 命中这些状态码就重试（覆盖 httpx.HTTPStatusError 与自研适配器的状态码文本）
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。attempts 是**总**尝试次数，不是额外重试次数。"""

    attempts: int = 3
    base_delay: float = 0.4
    max_delay: float = 4.0

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次失败后的等待时间（指数退避 + 抖动）。

        抖动是必要的：同一轮里模型可能并发调多个同类工具，
        没有抖动的话退避会同步，重试风暴反而把上游再打挂一次。
        """
        raw = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return raw * (0.5 + random.random() / 2)


@dataclass
class RetryStats:
    """单轮对话内的重试统计，用于日志与 SSE 的 end 事件。

    三个计数互不重叠：retries 是「重试了几次」，recovered 是「重试救回来了几次」，
    exhausted 是「重试完还是失败了几次」。
    """

    retries: int = 0
    exhausted: int = 0
    recovered: int = 0
    # 出过重试的工具名 → 次数，便于定位「哪个工具最不稳定」
    by_tool: dict[str, int] = field(default_factory=dict)

    def on_retry(self, tool: str) -> None:
        self.retries += 1
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "retries": self.retries,
            "exhausted": self.exhausted,
            "recovered": self.recovered,
            "by_tool": dict(self.by_tool),
        }


# 当前会话的重试统计；由 ChatService 在跑图之前设置，
# 与 current_broker / current_media 同一套模式
current_retry_stats: ContextVar[RetryStats | None] = ContextVar("current_retry_stats", default=None)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试。

    顺序即优先级：不可重试 → 明确的可重试类型 → 文本兜底。
    """
    # 取消必须原样上抛，不属于「失败」
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
        return False

    if isinstance(exc, MediaError):
        return exc.code in RETRYABLE_MEDIA_CODES

    # httpx 是最主要的失败来源，按类型判断比匹配文本可靠
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS

    if isinstance(exc, TimeoutError | ConnectionError):
        return True

    text = str(exc).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


async def call_with_retry(
    factory: Callable[[], Awaitable[Any]],
    *,
    policy: RetryPolicy,
    label: str = "tool",
    stats: RetryStats | None = None,
) -> Any:
    """按策略执行 ``factory``，失败时退避重试。

    ``factory`` 用可调用对象而不是协程：协程只能 await 一次，
    重试需要每次拿到一个新的。
    """
    attempts = max(1, policy.attempts)

    for attempt in range(1, attempts + 1):
        try:
            result = await factory()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            can_retry = attempt < attempts and is_retryable(exc)
            if not can_retry:
                # 只有「确实重试过」才算耗尽，一次就失败的不该计入
                if stats is not None and attempt > 1:
                    stats.exhausted += 1
                raise
            if stats is not None:
                stats.on_retry(label)
            delay = policy.delay_for(attempt)
            logger.warning(
                "工具调用失败，%.2fs 后重试 | tool=%s 第=%d/%d 次 原因=%s: %s",
                delay,
                label,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
            )
            await asyncio.sleep(delay)
        else:
            if attempt > 1:
                if stats is not None:
                    stats.recovered += 1
                logger.info("重试后成功 | tool=%s 第=%d 次", label, attempt)
            return result

    # 循环必然在 return 或 raise 处结束，这里只是让类型收窄有落点
    raise RuntimeError(f"{label} 调用未产生结果")


def make_resilient(tool: BaseTool, policy: RetryPolicy | None = None) -> BaseTool:
    """给现有工具套一层重试，保持名字/描述/参数 schema 与事件行为不变。

    不把重试写进业务工具里：那样每个工具都要抄一遍退避逻辑，
    「哪些失败该重试」也会分散在十几个文件里，改一次要改十几处。

    实现方式是**替换工具自身的 coroutine**，而不是在外面再包一个
    ``StructuredTool``。后者看起来更直观，但会产生嵌套的两次 tool run：
    ``astream_events`` 于是收到两个 ``on_tool_start``，上层按事件统计的
    「工具调用次数」和推给前端的 ``tool_call`` 事件都会翻倍。
    替换 coroutine 则只有一个工具对象，事件数量不变，
    而且参数校验、args_schema、描述都还是原来那一份。

    只处理 async 工具：本项目所有工具都是 async 的，同步工具不包装
    （包了就得在同步路径里 time.sleep，还会踩到上面那个嵌套问题）。
    """
    resolved = policy or RetryPolicy()
    if getattr(tool, _RESILIENT_MARK, False):
        return tool

    original = getattr(tool, "coroutine", None)
    if original is None:
        logger.debug("工具 %s 不是 async 实现，跳过重试包装", tool.name)
        return tool

    async def _call(*args: Any, **kwargs: Any) -> Any:
        return await call_with_retry(
            lambda: original(*args, **kwargs),
            policy=resolved,
            label=tool.name,
            stats=current_retry_stats.get(),
        )

    patched = tool.model_copy(update={"coroutine": _call})
    # pydantic 模型不接受未知字段赋值，这里直接写 __dict__ 打标记
    object.__setattr__(patched, _RESILIENT_MARK, True)
    return patched


def make_all_resilient(tools: list[BaseTool], policy: RetryPolicy | None = None) -> list[BaseTool]:
    return [make_resilient(tool, policy) for tool in tools]


__all__ = [
    "RetryPolicy",
    "RetryStats",
    "call_with_retry",
    "current_retry_stats",
    "is_retryable",
    "make_all_resilient",
    "make_resilient",
]
