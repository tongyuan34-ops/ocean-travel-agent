"""聊天 SSE 事件流：token / tool_call / tool_result / permission_request / end / error。

用假图替换真实 agent 图，因此不联网、不调模型——
这些用例校验的是**事件协议与状态映射**，不是模型能力。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.graph import SYSTEM_PROMPT
from app.agent.permissions import Decision, DecisionChannel, LocalDecisionChannel
from app.core.config import Settings
from app.services import chat as chat_module
from app.services.chat import ChatService
from app.services.streams import RedisEventLog
from app.storage import MemorySessionStore
from fake_redis import FakeRedis


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


def _service(
    monkeypatch,
    graph: Any,
    mode: str = "native",
    channel: DecisionChannel | None = None,
    event_log: Any = None,
) -> ChatService:
    monkeypatch.setattr(chat_module, "build_agent_graph", lambda _s: (graph, mode))
    from app.core.config import load_settings

    return ChatService(
        load_settings(),
        MemorySessionStore(),
        decision_channel=channel,
        event_log=event_log,
    )


def _other_worker(event_log: Any) -> ChatService:
    """另起一个 worker，**不**碰 ``build_agent_graph``。

    它只订阅/取消，永远不跑图；如果也去 monkeypatch，就会把前一个 worker
    的图覆盖掉——那样测出来的「跨进程」其实是同一个空图，用例会假装通过。
    """
    from app.core.config import load_settings

    return ChatService(load_settings(), MemorySessionStore(), event_log=event_log)


class FlakyLog:
    """事件日志抽风：``meta`` 或 ``tail`` 按脚本抛异常，其余接口照常。

    模拟的是真实撞到过的一次：重启后第一次对话，Redis 冷连接越过默认 5 秒
    读超时，SSE 生成器直接把异常抛出去，客户端收到 200 + 空响应体。
    """

    def __init__(self, fail_at: str = "meta") -> None:
        self._fail_at = fail_at

    async def create(self, stream_id: str, session_id: str) -> None: ...

    async def append(self, stream_id: str, payload: dict) -> int:
        return 1

    async def finish(self, stream_id: str, *, cancelled: bool = False) -> None: ...

    async def cancel(self, stream_id: str) -> bool:
        return True

    async def is_cancelled(self, stream_id: str) -> bool:
        return False

    async def aclose(self) -> None: ...

    async def meta(self, stream_id: str):
        if self._fail_at == "meta":
            raise ConnectionError("Timeout reading from localhost:6379")
        from app.services.streams import StreamMeta

        return StreamMeta(session_id="s", done=False)

    async def tail(self, stream_id: str, after: int, heartbeat: float = 15.0):
        # 先推一条再炸：模拟「回答推到一半存储断了」
        yield 1, {"type": "token", "content": "半截"}
        raise ConnectionError("Timeout reading from localhost:6379")


def _payloads(frames: list[str]) -> list[dict]:
    return [json.loads(frame.split("data: ", 1)[1]) for frame in frames if "data: " in frame]


async def test_stream_error_before_first_event_is_reported(monkeypatch):
    """存储挂了也要发一条 error 事件——静默空响应比报错更难查。"""
    service = _service(monkeypatch, FakeGraph([]), event_log=FlakyLog("meta"))
    frames = [frame async for frame in service.subscribe("s1", 0)]
    payloads = _payloads(frames)
    assert payloads[-1]["type"] == "error"
    assert payloads[-1]["code"] == "STREAM_UNAVAILABLE"


async def test_stream_error_midway_keeps_delivered_events(monkeypatch):
    """已经推出去的 token 不撤回，但必须补一条 error 说明为什么断了。"""
    service = _service(monkeypatch, FakeGraph([]), event_log=FlakyLog("tail"))
    frames = [frame async for frame in service.subscribe("s1", 0)]
    payloads = _payloads(frames)
    assert [item["type"] for item in payloads] == ["token", "error"]
    assert payloads[-1]["code"] == "STREAM_UNAVAILABLE"


async def _frames(service: ChatService, session_id: str, text: str) -> list[dict]:
    out: list[dict] = []
    async for frame in service.stream_reply(session_id, text):
        if "data: " in frame:
            out.append(json.loads(frame.split("data: ", 1)[1]))
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


async def test_cancel_stream_is_not_persisted(monkeypatch):
    """用户主动停止时应取消后台生成，不写入半截回答。"""
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
    state = await service.start_reply(session.id, "问题")
    assert await service.cancel_stream(state.stream_id) is True
    with contextlib.suppress(asyncio.CancelledError):
        await state.task

    stored = await service.get_session(session.id)
    assert all(item.role != "assistant" for item in stored.messages)


class SlowGraph:
    """每产一个 token 前等一下，用来制造「生成进行中」的窗口。"""

    def __init__(self, chunks: list[str], delay: float = 0.02) -> None:
        self._chunks = chunks
        self._delay = delay

    async def astream_events(
        self, _input: Any, version: str | None = None, config: Any = None
    ) -> AsyncIterator[dict[str, Any]]:
        for text in self._chunks:
            await asyncio.sleep(self._delay)
            yield {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(text)}}
        yield _final_message("".join(self._chunks))


async def test_resume_lands_on_another_worker(monkeypatch):
    """多 worker 的核心场景：生成在 A，断线后重连落到 B，靠 Last-Event-ID 接着推。

    两个 service 各持一个 EventLog 实例、共用同一份 Redis——这正是
    ``uvicorn --workers 2`` 下两个进程看到的世界。B 从来看不到 A 的内存，
    能续上完全靠共享存储。
    """
    client = FakeRedis()
    script = [
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("一")}},
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("二")}},
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("三")}},
        _final_message("一二三"),
    ]
    worker_a = _service(monkeypatch, FakeGraph(script), event_log=RedisEventLog(client))
    worker_b = _other_worker(RedisEventLog(client))

    session = await worker_a.create_session()
    state = await worker_a.start_reply(session.id, "问题")
    await state.task

    # 客户端只收到过第 1 个事件，重连时带上 Last-Event-ID=1
    resumed = [frame async for frame in worker_b.subscribe(state.stream_id, 1)]
    payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in resumed if "data: " in frame]
    assert _types(payloads) == ["token", "token", "end"]


async def test_cancel_from_another_worker_stops_generation(monkeypatch):
    """用户在 B 点停止，生成在 A：A 没有那条 Task，只能靠共享标记轮询退出。

    这是 ``cancel_stream`` 里「先写标记再 cancel 本地任务」两半中的后一半——
    只测同进程取消的话，这条路径永远不会被执行。
    """
    monkeypatch.setattr(chat_module, "CANCEL_POLL_SEC", 0.01)
    client = FakeRedis()
    worker_a = _service(
        monkeypatch, SlowGraph(["一", "二", "三", "四"]), event_log=RedisEventLog(client)
    )
    worker_b = _other_worker(RedisEventLog(client))

    session = await worker_a.create_session()
    state = await worker_a.start_reply(session.id, "问题")
    # 等 A 真的开始生成，再让 B 去取消
    for _ in range(200):
        if client.lists.get(f"chat:stream:{state.stream_id}:events"):
            break
        await asyncio.sleep(0.01)

    assert await worker_b.cancel_stream(state.stream_id) is True
    await asyncio.wait_for(state.task, timeout=5)

    stored = await worker_a.get_session(session.id)
    assert all(item.role != "assistant" for item in stored.messages), "取消后不该落库半截回答"
    meta = await worker_a._log.meta(state.stream_id)
    assert meta is not None and meta.cancelled is True


async def test_disconnect_does_not_cancel_generation(monkeypatch):
    """订阅者断开只停止推送；后台生成继续，最终回答仍会落库。"""
    service = _service(
        monkeypatch,
        FakeGraph(
            [
                {"event": "on_chat_model_stream", "data": {"chunk": "完整"}},
                _final_message("完整回答"),
            ]
        ),
    )
    session = await service.create_session()
    generator = service.stream_reply(session.id, "问题")
    first = await generator.__anext__()
    assert json.loads(first.split("data: ", 1)[1])["type"] == "token"
    await generator.aclose()
    await asyncio.sleep(0.2)

    stored = await service.get_session(session.id)
    assert stored.messages[-1].role == "assistant"
    assert stored.messages[-1].content == "完整回答"


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
    """有副作用的操作必须先出现在流里等用户决策，拿到放行后才继续。

    裁决走的是注入的 `DecisionChannel`，不再是「按 request_id 找一个 broker」——
    多进程下那次 POST 可能落在任何一台机器上，进程内的 dict 找不到它。
    """
    channel = LocalDecisionChannel()
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
        channel=channel,
    )
    session = await service.create_session()

    frames: list[dict] = []
    saw_request = False
    async for frame in service.stream_reply(session.id, "帮我写文件"):
        if "data: " not in frame:
            continue
        payload = json.loads(frame.split("data: ", 1)[1])
        frames.append(payload)
        if payload["type"] == "permission_request":
            saw_request = True
            assert payload["action"] == "write_file"
            assert payload["detail"]["path"] == "x.txt"
            decided = await channel.decide(payload["request_id"], Decision(granted=True))
            assert decided is True, "permission_request 必须能被裁决接口命中"

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
    # 必须传真实 system prompt：折叠预算按「system + 摘要 + 历史」估算，
    # 传空串会让 token 总量低于阈值而直接跳过摘要，用例就测不到失败路径了
    await service._refresh_summary(session, SYSTEM_PROMPT)

    # 失败时不推进 summarized_count，那批消息继续留在窗口里
    assert session.summary == ""
    assert session.summarized_count == 0
