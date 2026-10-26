"""工具重试与失败分类。

重点验证「什么不该重试」：权限拒绝与取消一旦被重试，
权限网关和用户中止都会失效，而且症状极难复现。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.permissions import PermissionDenied
from app.agent.resilience import (
    RetryPolicy,
    RetryStats,
    ToolBudget,
    call_with_retry,
    current_tool_budget,
    is_retryable,
    make_all_resilient,
    make_resilient,
)
from app.core.security import PathNotAllowed
from app.media.base import MediaError

FAST = RetryPolicy(attempts=3, base_delay=0.001, max_delay=0.005)


def test_permission_denied_is_never_retryable():
    """核心约束：用户拒绝不是瞬时故障，重试等于绕过确认。"""
    assert is_retryable(PermissionDenied("用户拒绝了该操作")) is False


def test_path_not_allowed_is_not_retryable():
    assert is_retryable(PathNotAllowed("越界")) is False


def test_cancelled_is_not_retryable():
    assert is_retryable(asyncio.CancelledError()) is False


def test_programmer_errors_are_not_retryable():
    for exc in (ValueError("bad args"), TypeError("wrong type"), KeyError("k")):
        assert is_retryable(exc) is False


def test_network_errors_are_retryable():
    assert is_retryable(TimeoutError()) is True
    assert is_retryable(ConnectionError()) is True
    assert is_retryable(httpx.ConnectTimeout("timeout")) is True
    assert is_retryable(httpx.ReadError("reset")) is True


def test_http_status_classification():
    request = httpx.Request("GET", "https://example.com")

    def status_error(code: int) -> httpx.HTTPStatusError:
        response = httpx.Response(code, request=request)
        return httpx.HTTPStatusError("boom", request=request, response=response)

    assert is_retryable(status_error(503)) is True
    assert is_retryable(status_error(429)) is True
    # 4xx 里除了限流都不该重试：参数错了再发一百次也一样
    assert is_retryable(status_error(400)) is False
    assert is_retryable(status_error(404)) is False


def test_media_error_classification():
    assert is_retryable(MediaError("MEDIA_UPSTREAM_TIMEOUT", "超时")) is True
    assert is_retryable(MediaError("MEDIA_BACKEND_MISSING", "没配")) is False


async def test_retries_then_succeeds():
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("upstream slow")
        return "ok"

    stats = RetryStats()
    result = await call_with_retry(flaky, policy=FAST, label="flaky", stats=stats)

    assert result == "ok"
    assert attempts["n"] == 3
    assert stats.retries == 2
    assert stats.recovered == 1
    assert stats.exhausted == 0
    assert stats.by_tool == {"flaky": 2}


async def test_exhausts_and_reraises_original_error():
    async def always_fail() -> str:
        raise TimeoutError("upstream slow")

    stats = RetryStats()
    with pytest.raises(TimeoutError):
        await call_with_retry(always_fail, policy=FAST, label="always", stats=stats)

    assert stats.retries == 2  # attempts=3，只重试 2 次
    assert stats.exhausted == 1
    assert stats.recovered == 0


async def test_permission_denied_fails_immediately():
    calls = {"n": 0}

    async def denied() -> str:
        calls["n"] += 1
        raise PermissionDenied("用户拒绝了该操作")

    stats = RetryStats()
    with pytest.raises(PermissionDenied):
        await call_with_retry(denied, policy=FAST, label="write_file", stats=stats)

    assert calls["n"] == 1
    assert stats.retries == 0


async def test_cancelled_error_propagates_without_retry():
    calls = {"n": 0}

    async def cancelled() -> str:
        calls["n"] += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await call_with_retry(cancelled, policy=FAST, label="x")

    assert calls["n"] == 1


async def test_first_attempt_success_counts_nothing():
    stats = RetryStats()

    async def fine() -> str:
        return "ok"

    assert await call_with_retry(fine, policy=FAST, label="fine", stats=stats) == "ok"
    assert stats.retries == 0
    assert stats.recovered == 0


class _Args(BaseModel):
    city: str = Field(description="城市")


@tool("probe_tool", args_schema=_Args)
async def probe_tool(city: str) -> str:
    """探针工具，用于验证包装后行为不变。"""
    return f"{city}-ok"


async def test_make_resilient_preserves_metadata():
    wrapped = make_resilient(probe_tool)

    assert wrapped.name == "probe_tool"
    assert wrapped.description == probe_tool.description
    assert wrapped.args_schema is _Args
    assert await wrapped.ainvoke({"city": "杭州"}) == "杭州-ok"


async def test_make_resilient_retries_inside_single_tool_call():
    """重试发生在工具内部，不应该产生第二个工具对象或额外事件。"""
    attempts = {"n": 0}

    @tool("flaky_tool", args_schema=_Args)
    async def flaky_tool(city: str) -> str:
        """会失败两次的工具。"""
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TimeoutError("slow")
        return f"{city}-ok"

    wrapped = make_resilient(flaky_tool, FAST)
    stats = RetryStats()
    token = _set_stats(stats)
    try:
        assert await wrapped.ainvoke({"city": "杭州"}) == "杭州-ok"
    finally:
        _reset_stats(token)

    assert attempts["n"] == 3
    assert stats.retries == 2


def test_make_resilient_is_idempotent():
    """graph 每次重建都会包一遍工具，重复包装会让重试次数翻倍。"""
    once = make_resilient(probe_tool)
    twice = make_resilient(once)
    assert twice is once


def test_make_all_resilient_returns_same_length():
    assert len(make_all_resilient([probe_tool])) == 1


def _set_stats(stats: RetryStats):
    from app.agent.resilience import current_retry_stats

    return current_retry_stats.set(stats)


def _reset_stats(token) -> None:
    from app.agent.resilience import current_retry_stats

    current_retry_stats.reset(token)


# ---------- 单轮工具预算 ----------


def test_budget_allows_until_per_tool_limit():
    budget = ToolBudget(per_tool=2, total=10)
    assert budget.consume("get_weather") == ""
    assert budget.consume("get_weather") == ""
    # 第三次开始拦截
    assert "上限" in budget.consume("get_weather")
    assert budget.blocked == 1
    # 别的工具不受影响
    assert budget.consume("plan_route") == ""


def test_budget_total_limit():
    budget = ToolBudget(per_tool=10, total=3)
    for _ in range(3):
        assert budget.consume("search_poi") == ""
    assert "总上限" in budget.consume("get_weather")
    assert budget.used == 3


def test_budget_zero_means_unlimited():
    budget = ToolBudget(per_tool=0, total=0)
    for _ in range(50):
        assert budget.consume("search_poi") == ""
    assert budget.blocked == 0


def test_budget_to_dict_counts_by_tool():
    budget = ToolBudget(per_tool=2, total=10)
    budget.consume("a")
    budget.consume("a")
    budget.consume("a")
    data = budget.to_dict()
    assert data["used"] == 2
    assert data["blocked"] == 1
    assert data["by_tool"] == {"a": 2}


async def test_budgeted_tool_returns_error_text_instead_of_executing():
    """超限时不能抛异常：整轮对话失败比「少调一次工具」更糟。"""
    calls = {"n": 0}

    @tool("budgeted_tool", args_schema=_Args)
    async def budgeted_tool(city: str) -> str:
        """会被预算拦住的工具。"""
        calls["n"] += 1
        return f"{city}-ok"

    wrapped = make_resilient(budgeted_tool)
    budget = ToolBudget(per_tool=1, total=10)
    token = current_tool_budget.set(budget)
    try:
        assert await wrapped.ainvoke({"city": "杭州"}) == "杭州-ok"
        blocked = await wrapped.ainvoke({"city": "杭州"})
    finally:
        current_tool_budget.reset(token)

    assert calls["n"] == 1  # 第二次没有真的执行
    assert blocked.startswith("TOOL_BUDGET_EXCEEDED")


async def test_budget_is_per_turn_not_global():
    """每轮都要新建 ToolBudget，否则第二句话会被第一句话的调用次数拖累。"""
    budget_one = ToolBudget(per_tool=1, total=10)
    budget_one.consume("get_weather")
    budget_one.consume("get_weather")
    assert budget_one.blocked == 1

    budget_two = ToolBudget(per_tool=1, total=10)
    assert budget_two.consume("get_weather") == ""
