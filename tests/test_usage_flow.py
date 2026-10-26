"""用量采集与预算闸在 SSE 流程里的行为。

这些用例用假图替换真实 agent 图，只验证「事件协议与持久化」，
不联网也不调模型。
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage

from app.core.config import load_settings
from app.services import chat as chat_module
from app.services.chat import ChatService
from app.storage import MemorySessionStore


class _Graph:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script

    async def astream_events(
        self, _input: Any, version: str | None = None, config: Any = None
    ) -> AsyncIterator[dict[str, Any]]:
        for event in self._script:
            yield event


def _model_end(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "event": "on_chat_model_end",
        "data": {
            "output": AIMessage(
                content="",
                usage_metadata={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
            )
        },
    }


def _final(text: str) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "data": {"output": {"messages": [AIMessage(content=text)]}},
    }


def _service(monkeypatch, graph: Any, **settings_overrides: Any) -> ChatService:
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda _s: (graph, "native"))
    settings = dataclasses.replace(load_settings(), **settings_overrides)
    return ChatService(settings, MemorySessionStore())


async def _frames(service: ChatService, session_id: str, text: str) -> list[dict]:
    out: list[dict] = []
    async for frame in service.stream_reply(session_id, text):
        if "data: " in frame:
            out.append(json.loads(frame.split("data: ", 1)[1]))
    return out


def _end(frames: list[dict]) -> dict:
    ends = [frame for frame in frames if frame.get("type") == "end"]
    assert len(ends) == 1
    return ends[0]


async def test_end_event_carries_real_usage(monkeypatch):
    graph = _Graph([_model_end(320, 48), _final("北京明天多云")])
    service = _service(monkeypatch, graph)
    session = await service.create_session()

    end = _end(await _frames(service, session.id, "北京天气"))

    usage = end["usage"]["turn"]
    assert usage["input_tokens"] == 320
    assert usage["output_tokens"] == 48
    assert usage["total_tokens"] == 368
    assert usage["estimated"] is False
    assert end["usage"]["session"]["total_tokens"] == 368
    assert end["usage"]["session"]["turns"] == 1


async def test_multiple_model_calls_accumulate(monkeypatch):
    """native 模式下工具往返会产生多次模型调用，必须逐次累加。"""
    graph = _Graph([_model_end(100, 10), _model_end(150, 20), _final("好的")])
    service = _service(monkeypatch, graph)
    session = await service.create_session()

    end = _end(await _frames(service, session.id, "问题"))

    assert end["usage"]["turn"]["calls"] == 2
    assert end["usage"]["turn"]["total_tokens"] == 280


async def test_missing_usage_falls_back_to_estimate(monkeypatch):
    """本地模型常不返回 usage，此时必须标记 estimated，不能假装精确。"""
    graph = _Graph([_final("杭州明天多云")])
    service = _service(monkeypatch, graph)
    session = await service.create_session()

    end = _end(await _frames(service, session.id, "杭州天气"))

    usage = end["usage"]["turn"]
    assert usage["estimated"] is True
    assert usage["total_tokens"] > 0


async def test_session_usage_accumulates_across_turns(monkeypatch):
    graph = _Graph([_model_end(100, 10), _final("一")])
    service = _service(monkeypatch, graph)
    session = await service.create_session()

    await _frames(service, session.id, "第一轮")
    await _frames(service, session.id, "第二轮")

    usage = await service.get_usage(session.id)
    assert usage["total_tokens"] == 220
    assert usage["turns"] == 2


async def test_message_persists_turn_usage(monkeypatch):
    graph = _Graph([_model_end(60, 6), _final("回答")])
    service = _service(monkeypatch, graph)
    session = await service.create_session()

    await _frames(service, session.id, "问题")

    stored = await service.get_session(session.id)
    assistant = [item for item in stored.messages if item.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].usage["total_tokens"] == 66


async def test_budget_gate_rejects_and_keeps_history_clean(monkeypatch):
    """超预算时连用户消息都不能落库，否则历史里会留下永远没有回答的一句话。"""
    graph = _Graph([_model_end(100, 10), _final("回答")])
    service = _service(monkeypatch, graph, max_tokens_per_session=100)
    session = await service.create_session()
    session.usage = {"total_tokens": 100, "turns": 3}
    await service._store.save(session)

    frames = await _frames(service, session.id, "还能再问吗")

    errors = [frame for frame in frames if frame.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["code"] == "BUDGET_EXCEEDED"

    stored = await service.get_session(session.id)
    assert stored.messages == []


async def test_budget_disabled_when_zero(monkeypatch):
    graph = _Graph([_model_end(100, 10), _final("回答")])
    service = _service(monkeypatch, graph, max_tokens_per_session=0)
    session = await service.create_session()
    # 形状要和 TokenUsage.to_dict 一致：只有 total_tokens 反推不出输入/输出
    session.usage = {"input_tokens": 10_000_000, "output_tokens": 0, "total_tokens": 10_000_000}
    await service._store.save(session)

    end = _end(await _frames(service, session.id, "还能再问吗"))
    assert end["usage"]["session"]["total_tokens"] > 10_000_000


async def test_end_event_reports_tool_retries(monkeypatch):
    graph = _Graph([_model_end(10, 1), _final("回答")])
    service = _service(monkeypatch, graph)
    session = await service.create_session()

    end = _end(await _frames(service, session.id, "问题"))

    retries = end["tool_retries"]
    assert retries["retries"] == 0
    assert retries["exhausted"] == 0
    assert retries["by_tool"] == {}
