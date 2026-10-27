"""事件流与裁决通道：内存 / Redis 两套实现必须语义一致。

这里最有价值的不是「内存版能跑」，而是**跨进程那两件事**：

- 续传时 ``Last-Event-ID`` 落在另一个进程上，还能接着推；
- 用户在 A 进程点「停止」/「允许」，生成在 B 进程里，标记要能过去。

单进程测试天然覆盖不到这两条，所以用替身 Redis 把两个实例摆在一起，
模拟「两个 worker 各持一个 EventLog、共用同一份 Redis」。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.permissions import Decision, RedisDecisionChannel
from app.services.streams import (
    MemoryEventLog,
    RedisEventLog,
    StreamState,
    build_event_log,
)
from fake_redis import FakeRedis


async def _drain(log, stream_id: str, after: int, limit: int, heartbeat: float = 5.0) -> list:
    """取到 ``limit`` 个事件就停，避免测试挂在无限 tail 上。"""
    out: list = []
    async for item in log.tail(stream_id, after, heartbeat=heartbeat):
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ==================== 内存实现 ====================


async def test_memory_replays_from_cursor():
    log = MemoryEventLog()
    await log.create("s1", "sess")
    for text in ("a", "b", "c"):
        await log.append("s1", {"type": "token", "text": text})
    await log.finish("s1")

    got = await _drain(log, "s1", after=1, limit=2)
    assert got == [(2, {"type": "token", "text": "b"}), (3, {"type": "token", "text": "c"})]


async def test_memory_tail_returns_when_finished():
    """流结束后订阅必须自己收尾，不能挂着——挂着的连接就是泄漏。"""
    log = MemoryEventLog()
    await log.create("s1", "sess")
    await log.append("s1", {"type": "token", "text": "a"})
    await log.finish("s1")

    seen = [item async for item in log.tail("s1", 0, heartbeat=0.05)]
    assert seen == [(1, {"type": "token", "text": "a"})]


async def test_memory_tail_heartbeats_when_idle():
    """空闲时产出 None（调用方据此发 ping），而不是一直沉默到代理掐线。"""
    log = MemoryEventLog()
    await log.create("s1", "sess")

    seen = await _drain(log, "s1", after=0, limit=1, heartbeat=0.01)
    assert seen == [None]


async def test_memory_cancel_is_visible_to_generator():
    log = MemoryEventLog()
    await log.create("s1", "sess")
    assert await log.is_cancelled("s1") is False

    assert await log.cancel("s1") is True
    assert await log.is_cancelled("s1") is True
    meta = await log.meta("s1")
    assert meta is not None and meta.cancelled is True


async def test_memory_cancel_unknown_stream_is_false():
    """取消一个不存在的流要返回 False，接口层据此回 404 而不是假装成功。"""
    assert await MemoryEventLog().cancel("nope") is False


async def test_memory_append_to_evicted_stream_does_not_raise():
    """流被淘汰后生成端还在推事件：丢掉就好，不能把整轮回答炸掉。"""
    log = MemoryEventLog()
    assert await log.append("ghost", {"type": "token"}) == 0


async def test_memory_prunes_oldest_stream():
    log = MemoryEventLog(max_streams=2)
    for stream_id in ("a", "b", "c"):
        await log.create(stream_id, "sess")

    assert await log.meta("a") is None
    assert await log.meta("b") is not None
    assert await log.meta("c") is not None


# ==================== Redis 实现 ====================


async def test_redis_roundtrip():
    log = RedisEventLog(FakeRedis())
    await log.create("s1", "sess")
    assert await log.append("s1", {"type": "token", "text": "a"}) == 1
    assert await log.append("s1", {"type": "token", "text": "b"}) == 2

    meta = await log.meta("s1")
    assert meta is not None and meta.session_id == "sess" and meta.done is False

    await log.finish("s1")
    meta = await log.meta("s1")
    assert meta is not None and meta.done is True


async def test_redis_resume_on_another_worker():
    """多 worker 的核心用例：生成在 A，重连续传落到 B。"""
    client = FakeRedis()
    worker_a = RedisEventLog(client)
    worker_b = RedisEventLog(client)

    await worker_a.create("s1", "sess")
    for text in ("a", "b", "c"):
        await worker_a.append("s1", {"type": "token", "text": text})
    await worker_a.finish("s1")

    # B 只看得到 Redis，看不到 A 的内存——这正是跨进程的样子
    got = [item async for item in worker_b.tail("s1", 2, heartbeat=0.05)]
    assert got == [(3, {"type": "token", "text": "c"})]


async def test_redis_append_renews_ttl():
    """只在 create 时设 TTL，长回答会在中途过期——续传就断了。

    这条防的是「写完才想起来 TTL」这类改动：一次 append 漏了续期，
    测试不会红，线上要等一条超过 1 小时的回答才会暴露。
    """
    client = FakeRedis()
    log = RedisEventLog(client)
    await log.create("s1", "sess")
    await log.append("s1", {"type": "token"})
    await log.append("s1", {"type": "token"})
    await log.append("s1", {"type": "token"})

    assert client.expires_for("chat:stream:s1:events") == 3


async def test_redis_cancel_from_another_worker():
    """用户在 A 点停止，生成在 B：B 只能靠轮询这个标记退出。"""
    client = FakeRedis()
    worker_a = RedisEventLog(client)
    worker_b = RedisEventLog(client)

    await worker_b.create("s1", "sess")
    assert await worker_b.is_cancelled("s1") is False

    assert await worker_a.cancel("s1") is True
    assert await worker_b.is_cancelled("s1") is True


async def test_redis_tail_stops_when_finished_elsewhere():
    """另一个进程收尾后，订阅端要能退出，而不是一直等心跳。"""
    client = FakeRedis()
    worker_a = RedisEventLog(client)
    worker_b = RedisEventLog(client)

    await worker_a.create("s1", "sess")
    await worker_a.append("s1", {"type": "token", "text": "a"})

    async def finish_later() -> None:
        await asyncio.sleep(0.02)
        await worker_a.finish("s1")

    task = asyncio.create_task(finish_later())
    try:
        got = [item async for item in worker_b.tail("s1", 0, heartbeat=0.05)]
    finally:
        await task

    assert got == [(1, {"type": "token", "text": "a"})]


async def test_redis_tail_unknown_stream_is_empty():
    log = RedisEventLog(FakeRedis())
    assert [item async for item in log.tail("ghost", 0, heartbeat=0.05)] == []


# ==================== 裁决通道 ====================


async def test_redis_decision_crosses_workers():
    """前端那次 POST 落到哪个进程都行——裁决写在共享 key 里。"""
    client = FakeRedis()
    waiting_worker = RedisDecisionChannel(client)
    deciding_worker = RedisDecisionChannel(client)

    await waiting_worker.open("req-1")

    async def decide_later() -> None:
        await asyncio.sleep(0.02)
        assert await deciding_worker.decide("req-1", Decision(granted=True)) is True

    task = asyncio.create_task(decide_later())
    try:
        decision = await waiting_worker.wait("req-1", timeout=2.0)
    finally:
        await task

    assert decision == Decision(granted=True)


async def test_redis_decision_unknown_request_is_rejected():
    channel = RedisDecisionChannel(FakeRedis())
    assert await channel.decide("nope", Decision(granted=True)) is False


async def test_redis_decision_timeout_returns_none():
    """超时返回 None，由 broker 收敛为拒绝——不是抛异常，也不是放行。"""
    channel = RedisDecisionChannel(FakeRedis())
    await channel.open("req-1")
    assert await channel.wait("req-1", timeout=0.05) is None


async def test_redis_decision_corrupt_payload_fails_closed():
    """key 里被人塞了垃圾：按拒绝处理，不能因为解析失败就默认放行。"""
    client = FakeRedis()
    channel = RedisDecisionChannel(client)
    await channel.open("req-1")
    client.strings["perm:req-1"] = "{not json"

    decision = await channel.wait("req-1", timeout=1.0)
    assert decision == Decision(granted=False)


async def test_redis_decision_close_removes_key():
    client = FakeRedis()
    channel = RedisDecisionChannel(client)
    await channel.open("req-1")
    await channel.close("req-1")
    assert await channel.decide("req-1", Decision(granted=True)) is False


# ==================== 工厂：选错实现的代价是「静默降级」 ====================


def _settings(storage_backend: str, shared_state: str):
    class _Settings:
        pass

    fake = _Settings()
    fake.storage_backend = storage_backend
    fake.shared_state = shared_state
    fake.redis_url = "redis://localhost:6379/0"
    return fake


@pytest.mark.parametrize(
    ("storage_backend", "shared_state", "expect_redis"),
    [
        ("memory", "auto", False),
        ("redis", "auto", True),
        ("memory", "redis", True),
        ("redis", "memory", False),
    ],
)
async def test_event_log_follows_storage_backend(storage_backend, shared_state, expect_redis):
    """``auto`` 必须跟着会话存储走：会话在 Redis、事件流在内存，
    会得到「会话在、续传不在」这种最难排查的半吊子状态。"""
    log = build_event_log(_settings(storage_backend, shared_state))
    try:
        assert isinstance(log, RedisEventLog if expect_redis else MemoryEventLog)
    finally:
        await log.aclose()


async def test_stream_state_marks_cancel_when_finishing():
    """``state.cancelled`` 一旦置位，收尾时也要把标记带进日志。"""
    client = FakeRedis()
    log = RedisEventLog(client)
    await log.create("s1", "sess")

    state = StreamState(stream_id="s1", session_id="sess", log=log)
    await state.emit({"type": "token", "text": "半截"})
    state.cancelled = True
    await state.finish()

    meta = await log.meta("s1")
    assert meta is not None and meta.cancelled is True and meta.done is True


async def test_events_are_json_encoded_for_redis():
    """事件里有中文和嵌套结构，编码坏了就是线上才发现的乱码。"""
    client = FakeRedis()
    log = RedisEventLog(client)
    await log.create("s1", "sess")
    payload = {"type": "permission_request", "detail": {"路径": "~/Desktop"}}
    await log.append("s1", payload)

    assert json.loads(client.lists["chat:stream:s1:events"][0]) == payload
