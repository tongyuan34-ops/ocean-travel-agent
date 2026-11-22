"""并发闸门：同一会话不并发、整体并发有上限。

为什么这两条值得单独立用例：

``ChatService`` 是进程内单例，会话存储的语义是「读整份 → 改 → 写回」。
同一会话上两轮同时跑，两边各自拿到一份副本、各自 append、各自 save，
后写的覆盖先写的——用户看到「我刚发的那条不见了」，而日志里两轮都成功。
这种丢数据不会抛异常，只能靠闸门从源头避免。

整体并发上限防的是另一件事：每一轮都是一次真实的模型调用，
前端连点、脚本重试、多个标签页同时发，没有上限就是无限并发。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app.core.errors import ApiError
from test_chat_sse import FakeGraph, _Chunk, _service


def _script(text: str) -> list[dict]:
    return [
        {"event": "on_chat_model_stream", "data": {"chunk": _Chunk(text)}},
        {"event": "on_chain_end", "data": {"output": {"messages": [AIMessage(content=text)]}}},
    ]


async def _drain(service, stream_id: str) -> None:
    """把一条流读到结束，确保后台任务收尾。"""
    async for _ in service.subscribe(stream_id, 0):
        pass


async def test_same_session_second_turn_is_rejected(monkeypatch):
    service = _service(monkeypatch, FakeGraph(_script("好")))
    session = await service._sessions.create()

    first = await service.start_reply(session.id, "第一条")

    # 第一条还没跑完（create_task 只是排进事件循环），这时候再发必须被挡住
    with pytest.raises(ApiError) as caught:
        await service.start_reply(session.id, "第二条")

    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.http_status == 429
    # 提示语要能让用户知道该做什么，而不是只看到一个 429
    assert "还在生成" in caught.value.message

    await _drain(service, first.stream_id)


async def test_session_is_free_again_after_reply(monkeypatch):
    """闸门必须释放，否则那个会话从此发不出消息——这比丢消息更难查。"""
    service = _service(monkeypatch, FakeGraph(_script("好")))
    session = await service._sessions.create()

    first = await service.start_reply(session.id, "第一条")
    await _drain(service, first.stream_id)

    second = await service.start_reply(session.id, "第二条")
    await _drain(service, second.stream_id)


async def test_slot_is_released_even_when_reply_crashes(monkeypatch):
    """生成炸了也要放闸：异常路径漏放，会话就被永久锁死。"""

    class ExplodingGraph:
        async def astream_events(self, _input, **_kw):
            raise RuntimeError("图内部炸了")
            yield {}  # pragma: no cover

    service = _service(monkeypatch, ExplodingGraph())
    session = await service._sessions.create()

    first = await service.start_reply(session.id, "会炸的一轮")
    await _drain(service, first.stream_id)

    # 换回正常图，确认同一个会话还能继续用
    from app.services import chat as chat_module

    monkeypatch.setattr(
        chat_module, "build_agent_graph", lambda _s, **_kw: (FakeGraph(_script("好")), "native")
    )
    second = await service.start_reply(session.id, "再试一次")
    await _drain(service, second.stream_id)


async def test_global_cap_rejects_the_extra_reply(monkeypatch):
    """整体并发上限：第 N+1 轮直接被拒，而不是排队占着内存。"""
    monkeypatch.setenv("MAX_CONCURRENT_REPLIES", "2")
    service = _service(monkeypatch, FakeGraph(_script("好")))

    sessions = [await service._sessions.create() for _ in range(3)]
    streams = [
        await service.start_reply(sessions[0].id, "一"),
        await service.start_reply(sessions[1].id, "二"),
    ]

    with pytest.raises(ApiError) as caught:
        await service.start_reply(sessions[2].id, "三")

    assert caught.value.code == "RATE_LIMITED"
    assert "上限" in caught.value.message

    for stream in streams:
        await _drain(service, stream.stream_id)
