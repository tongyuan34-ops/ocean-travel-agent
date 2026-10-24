"""链路追踪：trace_id 的生成、上下文传播，以及它是否真的落到了 SSE 事件上。

这一层不做「追踪上报是否成功」的断言——那取决于是否配了 LangSmith Key，
在 CI 里不可控。这里只校验**本地链路标识本身**是正确的。
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage

from app.core import tracing
from app.core.config import load_settings
from app.services import chat as chat_module
from app.services.chat import ChatService
from app.storage import MemorySessionStore

_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def test_new_trace_id_is_short_hex():
    trace_id = tracing.new_trace_id()
    assert _HEX16.match(trace_id)
    # 两次生成不能撞，否则同一份日志里两轮对话会被认成同一轮
    assert trace_id != tracing.new_trace_id()


def test_trace_context_is_nestable():
    assert tracing.get_trace_id() == ""

    outer = tracing.set_trace("a" * 16)
    assert tracing.get_trace_id() == "a" * 16

    inner = tracing.set_trace("b" * 16)
    assert tracing.get_trace_id() == "b" * 16

    tracing.reset_trace(inner)
    assert tracing.get_trace_id() == "a" * 16

    tracing.reset_trace(outer)
    assert tracing.get_trace_id() == ""


def test_trace_config_carries_run_name_and_metadata():
    config = tracing.trace_config("abc123", session_id="s1", user_id="")
    assert config["run_name"] == "chat-abc123"
    assert config["metadata"]["trace_id"] == "abc123"
    assert config["metadata"]["session_id"] == "s1"
    # 空值不该进 metadata，否则 LangSmith 面板上全是空字段
    assert "user_id" not in config["metadata"]


class _Graph:
    """只回一条最终消息的最小图，用来驱动一轮完整的 SSE。"""

    def __init__(self) -> None:
        self.configs: list[dict] = []

    async def astream_events(self, _input, version=None, config=None):
        self.configs.append(config or {})
        yield {
            "event": "on_chain_end",
            "data": {"output": {"messages": [AIMessage(content="好的")]}},
        }


async def _run_once(monkeypatch) -> tuple[list[dict], dict]:
    graph = _Graph()
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda _s: (graph, "native"))
    service = ChatService(load_settings(), MemorySessionStore())
    session = await service.create_session()

    frames: list[dict] = []
    async for frame in service.stream_reply(session.id, "你好"):
        if "data: " in frame:
            frames.append(json.loads(frame.split("data: ", 1)[1]))
    return frames, graph.configs[-1]


async def test_end_event_exposes_trace_id(monkeypatch):
    frames, config = await _run_once(monkeypatch)

    end = frames[-1]
    assert end["type"] == "end"
    assert _HEX16.match(end["trace_id"])

    # 同一个 id 必须同时出现在 SSE 事件和图运行的 config 里，
    # 否则日志与 LangSmith trace 对不上号，追踪就白做了
    assert config["run_name"] == f"chat-{end['trace_id']}"
    assert config["metadata"]["trace_id"] == end["trace_id"]


async def test_each_turn_gets_a_fresh_trace_id(monkeypatch):
    first, _ = await _run_once(monkeypatch)
    second, _ = await _run_once(monkeypatch)
    assert first[-1]["trace_id"] != second[-1]["trace_id"]
