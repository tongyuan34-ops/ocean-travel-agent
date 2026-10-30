"""SSE 事件流：生成与连接解耦，支持 Last-Event-ID 续传。

抽象出来的是**事件日志**（`EventLog`），而不是「进程里的一堆 asyncio.Condition」：

- `MemoryEventLog`：单进程，`asyncio.Condition` 唤醒，零依赖；
- `RedisEventLog`：多进程，List 存事件 + Pub/Sub 唤醒。`uvicorn --workers > 1`
  时重连续传会落到别的进程上，那时只有共享存储救得回来。

**为什么用 List 而不是 Redis Stream**：游标就是列表下标，和 SSE 的
`Last-Event-ID` 一一对应，`LRANGE key cursor -1` 一次取完。换成 XADD/XRANGE
的话事件 id 是 `毫秒-序号` 字符串，续传前还得把客户端传来的整数翻译成 stream id，
多一层换算就多一处能错的地方。这里要的是「按序重放」，不是「消费组 + ACK」。

**为什么 Pub/Sub 只做唤醒、不做真相**：Pub/Sub 会丢消息（订阅前发布、断线期间发布），
所以每次被唤醒都回 List 重读一遍。唤醒丢了也不致命——最坏退化成心跳间隔的轮询。

代价（写进 README 已知限制）：流状态有 TTL，过期后无法续传，只能提示刷新会话；
已完成的回答本身在会话存储里，不会因此丢失。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 流状态保留时长：够用户断线重连、刷新页面即可，不必留一天
STREAM_TTL_SEC = 3600
# 单进程最多保留多少条流，超出按最后更新时间淘汰
MAX_STREAMS = 200
# 订阅端空闲多久发一次心跳。公网反代会掐断长时间无数据的 SSE 连接
HEARTBEAT_SEC = 15.0
# 生成端多久检查一次「别的进程有没有取消我」
CANCEL_POLL_SEC = 1.0
# 唤醒频道里的控制消息（其余消息都是 seq 数字）
_CONTROL_WAKES = frozenset({"done", "cancel"})


@dataclass(frozen=True)
class StreamMeta:
    """一条流的元信息。不含事件本身——事件可能有上千条，多数场景不需要。"""

    session_id: str
    done: bool = False
    cancelled: bool = False


class EventLog(Protocol):
    """一次回答对应一条流，事件带从 1 开始的自增 seq。"""

    async def create(self, stream_id: str, session_id: str) -> None: ...

    async def append(self, stream_id: str, payload: dict[str, Any]) -> int: ...

    async def finish(self, stream_id: str, *, cancelled: bool = False) -> None: ...

    async def meta(self, stream_id: str) -> StreamMeta | None: ...

    async def cancel(self, stream_id: str) -> bool: ...

    async def is_cancelled(self, stream_id: str) -> bool: ...

    def tail(
        self, stream_id: str, after: int, heartbeat: float = HEARTBEAT_SEC
    ) -> AsyncIterator[tuple[int, dict[str, Any]] | None]:
        """按序产出 ``(seq, payload)``；空闲超过 heartbeat 秒产出一个 ``None``。

        ``None`` 是让调用方发心跳的信号——空闲本身不是事件，
        但如果把「等」放在调用方，调用方就得知道每种后端的等待方式。
        """
        ...

    async def aclose(self) -> None: ...


# ==================== 单进程实现 ====================


@dataclass
class _MemStream:
    session_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    updated_at: float = field(default_factory=time.time)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class MemoryEventLog:
    """进程内事件日志。单 worker 下够用，多 worker 会各看各的。"""

    def __init__(self, max_streams: int = MAX_STREAMS) -> None:
        self._streams: dict[str, _MemStream] = {}
        self._max = max_streams

    async def create(self, stream_id: str, session_id: str) -> None:
        self._prune()
        self._streams[stream_id] = _MemStream(session_id=session_id)

    async def append(self, stream_id: str, payload: dict[str, Any]) -> int:
        stream = self._streams.get(stream_id)
        if stream is None:
            # 流已被淘汰或从未创建：丢掉事件而不是抛异常，
            # 一轮回答不该因为日志丢了就崩掉
            logger.warning(
                "事件流不存在，事件被丢弃 | stream=%s type=%s", stream_id, payload.get("type")
            )
            return 0
        async with stream.condition:
            stream.events.append(payload)
            seq = len(stream.events)
            stream.updated_at = time.time()
            stream.condition.notify_all()
            return seq

    async def finish(self, stream_id: str, *, cancelled: bool = False) -> None:
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        async with stream.condition:
            stream.done = True
            stream.cancelled = stream.cancelled or cancelled
            stream.updated_at = time.time()
            stream.condition.notify_all()

    async def meta(self, stream_id: str) -> StreamMeta | None:
        stream = self._streams.get(stream_id)
        if stream is None:
            return None
        return StreamMeta(
            session_id=stream.session_id, done=stream.done, cancelled=stream.cancelled
        )

    async def cancel(self, stream_id: str) -> bool:
        stream = self._streams.get(stream_id)
        if stream is None:
            return False
        async with stream.condition:
            stream.cancelled = True
            stream.updated_at = time.time()
            stream.condition.notify_all()
        return True

    async def is_cancelled(self, stream_id: str) -> bool:
        stream = self._streams.get(stream_id)
        return bool(stream and stream.cancelled)

    async def tail(
        self, stream_id: str, after: int, heartbeat: float = HEARTBEAT_SEC
    ) -> AsyncIterator[tuple[int, dict[str, Any]] | None]:
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        cursor = max(0, after)
        while True:
            async with stream.condition:
                pending = stream.events[cursor:]
                done = stream.done
            if pending:
                for payload in pending:
                    cursor += 1
                    yield cursor, payload
                continue
            if done:
                return
            async with stream.condition:
                try:
                    await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat)
                except TimeoutError:
                    pass
                else:
                    continue
            yield None

    async def aclose(self) -> None:
        self._streams.clear()

    def _prune(self) -> None:
        """结束超过 TTL 的流不再保留；同时限制总量，避免内存无限增长。"""
        now = time.time()
        for stream_id, stream in list(self._streams.items()):
            if stream.done and now - stream.updated_at > STREAM_TTL_SEC:
                self._streams.pop(stream_id, None)
        if len(self._streams) >= self._max:
            oldest = sorted(self._streams.values(), key=lambda item: item.updated_at)
            for stream in oldest[: len(self._streams) - self._max + 1]:
                for stream_id, candidate in list(self._streams.items()):
                    if candidate is stream:
                        self._streams.pop(stream_id, None)
                        break


# ==================== 多进程实现 ====================


class RedisEventLog:
    """Redis 事件日志：List 存事件，Pub/Sub 唤醒订阅者。

    三条 key 共享一个前缀，一起设 TTL：
    ``{p}{id}:events`` 是事件列表（下标即 seq-1），``{p}{id}:meta`` 是状态 Hash，
    ``{p}{id}:wake`` 是唤醒频道。
    """

    def __init__(
        self, client: Any, ttl_sec: int = STREAM_TTL_SEC, prefix: str = "chat:stream:"
    ) -> None:
        self._client = client
        self._ttl = ttl_sec
        self._prefix = prefix

    def _events_key(self, stream_id: str) -> str:
        return f"{self._prefix}{stream_id}:events"

    def _meta_key(self, stream_id: str) -> str:
        return f"{self._prefix}{stream_id}:meta"

    def _wake_key(self, stream_id: str) -> str:
        return f"{self._prefix}{stream_id}:wake"

    async def create(self, stream_id: str, session_id: str) -> None:
        events_key = self._events_key(stream_id)
        meta_key = self._meta_key(stream_id)
        pipe = self._client.pipeline()
        pipe.delete(events_key)
        pipe.hset(meta_key, mapping={"session_id": session_id, "done": "0", "cancelled": "0"})
        pipe.expire(meta_key, self._ttl)
        await pipe.execute()

    async def append(self, stream_id: str, payload: dict[str, Any]) -> int:
        events_key = self._events_key(stream_id)
        seq = int(await self._client.rpush(events_key, json.dumps(payload, ensure_ascii=False)))
        # 每条都续期：一条流可能活很久，只在 create 时设 TTL 会让长回答中途过期
        await self._client.expire(events_key, self._ttl)
        await self._client.publish(self._wake_key(stream_id), str(seq))
        return seq

    async def finish(self, stream_id: str, *, cancelled: bool = False) -> None:
        meta_key = self._meta_key(stream_id)
        await self._client.hset(meta_key, "done", "1")
        if cancelled:
            await self._client.hset(meta_key, "cancelled", "1")
        await self._client.expire(meta_key, self._ttl)
        await self._client.publish(self._wake_key(stream_id), "done")

    async def meta(self, stream_id: str) -> StreamMeta | None:
        raw = await self._client.hgetall(self._meta_key(stream_id))
        if not raw:
            return None
        return StreamMeta(
            session_id=str(raw.get("session_id") or ""),
            done=raw.get("done") == "1",
            cancelled=raw.get("cancelled") == "1",
        )

    async def cancel(self, stream_id: str) -> bool:
        meta_key = self._meta_key(stream_id)
        if not await self._client.exists(meta_key):
            return False
        await self._client.hset(meta_key, "cancelled", "1")
        await self._client.publish(self._wake_key(stream_id), "cancel")
        return True

    async def is_cancelled(self, stream_id: str) -> bool:
        return await self._client.hget(self._meta_key(stream_id), "cancelled") == "1"

    async def tail(
        self, stream_id: str, after: int, heartbeat: float = HEARTBEAT_SEC
    ) -> AsyncIterator[tuple[int, dict[str, Any]] | None]:
        events_key = self._events_key(stream_id)
        if await self.meta(stream_id) is None:
            return
        cursor = max(0, after)
        pubsub = self._client.pubsub()
        try:
            await pubsub.subscribe(self._wake_key(stream_id))
            while True:
                items = await self._client.lrange(events_key, cursor, -1)
                for raw in items:
                    cursor += 1
                    yield cursor, json.loads(raw)
                meta = await self.meta(stream_id)
                if meta is None or meta.done:
                    return
                if items:
                    # 刚推过一批，立刻再看一次，别白等一个心跳
                    continue
                # get_message 的 timeout 偶发不生效，外面再套一层保险。
                # 超时是正常路径（这一轮就是没新事件），不是错误
                message = None
                with contextlib.suppress(TimeoutError):
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=heartbeat),
                        timeout=heartbeat + 1,
                    )
                # 收尾/取消信号不是新事件：回到循环顶部重读一次就会看到 done 并退出，
                # 这里再发一个心跳纯属多余（客户端会在 end 之后又收到一个 ping）
                if message and message.get("data") in _CONTROL_WAKES:
                    continue
                # 被唤醒可能只是因为 finish；这轮确实没新事件才发心跳
                if not await self._client.lrange(events_key, cursor, -1):
                    yield None
        finally:
            try:
                await pubsub.unsubscribe(self._wake_key(stream_id))
                await pubsub.aclose()
            except Exception:
                logger.debug("关闭 pubsub 时出错 | stream=%s", stream_id, exc_info=True)

    async def aclose(self) -> None:
        await self._client.aclose()


# ==================== 对外句柄与工厂 ====================


@dataclass
class StreamState:
    """一轮回答的句柄。

    真正的状态在 ``EventLog`` 里，这里只带 id、日志引用和**本进程**的后台任务。
    ``task`` 为 None 说明这一轮不是在当前进程里生成的——跨进程时只能靠
    ``log.cancel()`` 写标记，由生成端自己轮询退出。
    """

    stream_id: str
    session_id: str
    log: EventLog
    task: asyncio.Task | None = None
    cancelled: bool = False

    async def emit(self, payload: dict[str, Any]) -> int:
        return await self.log.append(self.stream_id, payload)

    async def finish(self, *, cancelled: bool = False) -> None:
        await self.log.finish(self.stream_id, cancelled=cancelled or self.cancelled)


def build_event_log(settings: Any) -> EventLog:
    """按 ``SHARED_STATE`` 选择实现。

    ``auto`` 的规则：会话已经放 Redis 就跟着放 Redis——
    既然部署时已经接受了「要有 Redis」，事件流再单进程存着只会制造一种
    「会话在、续传不在」的半吊子状态，比干脆不支持多进程更难排查。
    """
    mode = (getattr(settings, "shared_state", "auto") or "auto").lower()
    use_redis = mode == "redis" or (mode == "auto" and settings.storage_backend == "redis")
    if not use_redis:
        logger.info("事件流：内存（单进程；--workers > 1 时续传会失效）")
        return MemoryEventLog()

    from app.core.redis import async_redis

    client = async_redis(settings.redis_url)
    logger.info("事件流：Redis（%s，TTL=%ss）", settings.redis_url, STREAM_TTL_SEC)
    return RedisEventLog(client)
