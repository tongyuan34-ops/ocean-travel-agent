"""聊天 SSE 事件流：token / tool_call / tool_result / permission_request / end / error。

用假图替换真实 agent 图，因此不联网、不调模型——
这些用例校验的是**事件协议与状态映射**，不是模型能力。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import pytest
from langchain_core.messages import AIMessage

from app.agent.permissions import find_broker
from app.core.config import Settings
from app.services import chat as chat_module
from app.services.chat import ChatService
from app.storage import MemorySessionStore


class _Chunk:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeGraph:
    """按脚本逐条产出 astream_events 事件。

    脚本里可以放 `{"event": "__permission__"}`，表示在这一步发起一次权限申请，
    用来验证 permission_request 事件与后续放行是连起来的。
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = script

    async def astream_events(
        self, _input: Any, version: str | None = None, config: Any = None
    ) -> AsyncIterator[dict[str, Any]]:
        for event in self._script:
            if event.get("event") == "__permission__":
                from app.agent.permissions import require_permission

                await require_permission("write_file", {"path": "x.txt"}, "k")
                continue
            yield event


class ExplodingGraph:
    async def astream_events(self, _input: Any, **_: Any) -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("图内部炸了")
        yield {}  # pragma: no cover - 让函数成为异步生成器


def _service(monkeypatch, graph: Any, mode: str = "native") -> ChatService:
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda _s: (graph, mode))
    from app.core.config import load_settings

    return ChatService(load_settings(), MemorySessionStore())


async def _frames(service: ChatService, session_id: str, text: str) -> list[dict]:
    out: list[dict] = []
    async for frame in service.stream_reply(session_id, text):
        if frame.startswith("data: "):
            out.append(json.loads(frame[6:]))
    return out


def _final_message(text: str) -> dict:
    return {
        "event": "on_chain_end",
        "data": {"output": {"messages": [AIMessage(content=text)]}},
    }


def _types(frames: list[dict]) -> list[str]:
    return [frame["type"] for frame in frames]


async def test_streams_tokens_then_end(monkeypatch):
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("三亚")}},
                {"event": "on_chat_model_stream", "data": {"chunk": "攻略"}},
                _final_message("三亚攻略"),
            ]
        ),
    )
    session = await service.create_session()
    frames = await _frames(service, session.id, "三亚怎么玩")

    assert _types(frames)[:2] == ["token", "token"]
    end = frames[-1]
    assert end["type"] == "end"
    assert end["finish_reason"] == "stop"

    # 回答必须落库，刷新页面才能恢复
    stored = await service.get_session(session.id)
    assert stored.messages[-1].role == "assistant"
    assert stored.messages[-1].content == "三亚攻略"


async def test_stops_halfway_by_abort_is_not_persisted(monkeypatch):
    """客户端中断（生成器未被消费完）时不应写入半截回答。"""
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {"event": "on_chat_model_stream", "data": {"chunk": "半截"}},
                _final_message("半截回答"),
            ]
        ),
    )
    session = await service.create_session()
    generator = service.stream_reply(session.id, "问题")
    first = await generator.__anext__()
    assert json.loads(first[6:])["type"] == "token"
    await generator.aclose()

    stored = await service.get_session(session.id)
    assert all(item.role != "assistant" for item in stored.messages)


async def test_tool_call_and_result_error_status(monkeypatch):
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {
                    "event": "on_tool_start",
                    "name": "search_social_session",
                    "run_id": "r1",
                    "data": {"input": {"query": "三亚"}},
                },
                {
                    "event": "on_tool_end",
                    "run_id": "r1",
                    "data": {"output": "TOOL_NOT_ENABLED：社媒采集默认关闭"},
                },
                _final_message("没拿到内容"),
            ]
        ),
    )
    session = await service.create_session()
    frames = await _frames(service, session.id, "搜社媒")

    call = next(f for f in frames if f["type"] == "tool_call")
    assert call["category"] == "plugin"
    assert call["tool"] == "search_social_session"
    assert call["call_id"] == "r1"

    result = next(f for f in frames if f["type"] == "tool_result")
    assert result["status"] == "error"
    assert result["summary"].startswith("TOOL_NOT_ENABLED")


async def test_tool_result_ok_status(monkeypatch):
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {
                    "event": "on_tool_start",
                    "name": "search_web",
                    "run_id": "r2",
                    "data": {"input": {"query": "三亚"}},
                },
                {
                    "event": "on_tool_end",
                    "run_id": "r2",
                    "data": {"output": "检索「三亚」 统计：命中源数 1｜耗时 1.2s"},
                },
                _final_message("找到了"),
            ]
        ),
    )
    session = await service.create_session()
    frames = await _frames(service, session.id, "搜网页")

    call = next(f for f in frames if f["type"] == "tool_call")
    assert call["category"] == "search"

    result = next(f for f in frames if f["type"] == "tool_result")
    assert result["status"] == "ok"


async def test_tool_error_event(monkeypatch):
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {
                    "event": "on_tool_error",
                    "run_id": "r3",
                    "data": {"error": "boom"},
                },
                _final_message("工具失败了"),
            ]
        ),
    )
    session = await service.create_session()
    frames = await _frames(service, session.id, "触发错误")
    result = next(f for f in frames if f["type"] == "tool_result")
    assert result["status"] == "error"
    assert "boom" in result["summary"]


async def test_permission_request_then_granted(monkeypatch):
    """有副作用的操作必须先出现在流里等用户决策，拿到放行后才继续。"""
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {"event": "__permission__"},
                {
                    "event": "on_tool_end",
                    "run_id": "r4",
                    "data": {"output": "已写入 x.txt"},
                },
                _final_message("写好了"),
            ]
        ),
    )
    session = await service.create_session()

    frames: list[dict] = []
    saw_request = False
    async for frame in service.stream_reply(session.id, "帮我写文件"):
        payload = json.loads(frame[6:])
        frames.append(payload)
        if payload["type"] == "permission_request":
            saw_request = True
            assert payload["action"] == "write_file"
            assert payload["detail"]["path"] == "x.txt"
            broker = find_broker(payload["request_id"])
            assert broker is not None, "permission_request 必须能被裁决接口找到"
            assert broker.resolve(payload["request_id"], granted=True) is True

    assert saw_request
    assert _types(frames)[-1] == "end"
    result = next(f for f in frames if f["type"] == "tool_result")
    assert result["status"] == "ok"


async def test_empty_response_maps_to_contract_error(monkeypatch):
    service = _service(monkeypatch, FakeGraph([]))
    session = await service.create_session()
    frames = await _frames(service, session.id, "问点什么")

    assert frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "MODEL_EMPTY_RESPONSE"


async def test_model_unavailable_when_graph_build_fails(monkeypatch):
    def boom(_settings: Settings):
        raise RuntimeError("连不上模型")

    monkeypatch.setattr(chat_module, "build_agent_graph", boom)
    from app.core.config import load_settings

    service = ChatService(load_settings(), MemorySessionStore())
    session = await service.create_session()
    frames = await _frames(service, session.id, "在吗")

    assert frames[0]["type"] == "error"
    assert frames[0]["code"] == "MODEL_UNAVAILABLE"


async def test_internal_error_when_graph_crashes(monkeypatch):
    service = _service(monkeypatch, ExplodingGraph())
    session = await service.create_session()
    frames = await _frames(service, session.id, "触发崩溃")

    assert frames[-1]["type"] == "error"
    assert frames[-1]["code"] == "INTERNAL_ERROR"


async def test_summary_failure_keeps_history(monkeypatch):
    """摘要失败不能丢历史：宁可多占上下文，也不能让记忆凭空消失（SPEC 4.5e）。"""
    from app.storage import ChatMessage

    service = _service(monkeypatch, FakeGraph([]))
    session = await service.create_session()
    for index in range(40):
        session.messages.append(
            ChatMessage(
                id=str(index),
                role="user" if index % 2 == 0 else "assistant",
                content=f"第{index}条对话内容",
                created_at="",
            )
        )

    class BoomModel:
        async def ainvoke(self, _prompt):
            raise RuntimeError("摘要服务挂了")

    service._summarizer = BoomModel()
    await service._refresh_summary(session)

    # 失败时不推进 summarized_count，那批消息继续留在窗口里
    assert session.summary == ""
    assert session.summarized_count == 0
