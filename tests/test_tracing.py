"""链路追踪：trace_id 的生成、上下文传播，以及它是否真的落到了 SSE 事件上。

这一层不做「追踪上报是否成功」的断言——那取决于是否配了 LangSmith Key，
在 CI 里不可控。这里只校验**本地链路标识本身**是正确的。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

from app.core import tracing
from app.core.config import load_settings
from app.services import chat as chat_module
from app.services.chat import ChatService
from app.services.sessions import SessionService
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
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda _s, **_kw: (graph, "native"))
    service = ChatService(load_settings(), SessionService(MemorySessionStore()))
    session = await service._sessions.create()

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


# ---------- Langfuse（可选后端） ----------


@pytest.fixture
def fresh_langfuse(monkeypatch):
    """把模块里的追踪状态清回初始值。

    ``configure_langfuse`` 只尝试一次并记住结论（避免每轮重试超时），
    所以用例之间必须显式复位，否则第二个用例拿到的是上一个用例的结果。
    """
    monkeypatch.setattr(tracing, "_langfuse_settled", False)
    monkeypatch.setattr(tracing, "_langfuse_client", None)
    monkeypatch.setattr(tracing, "_langfuse_handler", None)


def test_langfuse_without_keys_is_a_no_op(fresh_langfuse, settings):
    """没配 key 就完全空转：不建客户端、不挂回调、不发请求。"""
    bare = replace(settings, langfuse_public_key="", langfuse_secret_key="")

    assert tracing.configure_langfuse(bare) is False
    assert tracing.langfuse_callbacks() == []
    assert tracing.langfuse_enabled() is False


def test_langfuse_can_be_switched_off_with_keys_present(fresh_langfuse, settings):
    """只填了 key 但不想上报时，开关要能压住它。"""
    off = replace(
        settings,
        langfuse_tracing=False,
        langfuse_public_key="pk-lf-x",
        langfuse_secret_key="sk-lf-x",
    )

    assert tracing.configure_langfuse(off) is False
    assert tracing.langfuse_callbacks() == []


def test_flush_and_shutdown_are_safe_when_disabled(fresh_langfuse):
    """退出路径上不能抛：追踪没配好也不该让程序关不掉。"""
    tracing.flush_tracing()
    tracing.shutdown_tracing()
    assert tracing.langfuse_enabled() is False


def test_trace_config_maps_session_to_langfuse_keys(fresh_langfuse, monkeypatch):
    """启用后 metadata 要同时带 LangSmith 与 Langfuse 两套键。

    Langfuse 只认 langfuse_session_id / langfuse_user_id 这几个名字，
    少一个键的结果就是面板上所有 trace 散着，按会话根本串不起来。
    """
    monkeypatch.setattr(tracing, "_langfuse_handler", object())

    config = tracing.trace_config("abc123", session_id="s1", user_id="u1")

    assert config["metadata"]["langfuse_session_id"] == "s1"
    assert config["metadata"]["langfuse_user_id"] == "u1"
    assert config["metadata"]["langfuse_trace_name"] == "chat-abc123"
    # 原来给 LangSmith 的键不能被顶掉
    assert config["metadata"]["session_id"] == "s1"
    assert config["run_name"] == "chat-abc123"


def test_trace_config_stays_clean_when_disabled(fresh_langfuse):
    """没启用时不要把 langfuse_* 塞进 metadata——LangSmith 面板上会全是无用字段。"""
    config = tracing.trace_config("abc123", session_id="s1")
    assert not any(key.startswith("langfuse_") for key in config["metadata"])
