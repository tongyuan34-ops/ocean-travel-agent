"""内存版 Redis 双精度替身，够 ``RedisEventLog`` / ``RedisDecisionChannel`` 用。

为什么不用真 Redis：CI 里起容器会让单测从「几百毫秒」变成「几十秒 + 一个外部依赖」，
而这里要验的恰恰是**代码对 Redis 语义的假设**（List 下标即 seq、Pub/Sub 会丢消息、
key 才是真相），用替身反而更容易构造这些边界。

替身只实现被用到的那几个命令。故意**不**实现 TTL 过期——过期是 Redis 的职责，
我们关心的是「有没有去续期」，所以把 ``expire`` 调用记下来供断言。
"""

from __future__ import annotations

import asyncio
from typing import Any


class FakePubSub:
    """订阅端。每个实例一条队列，publish 时把消息塞进所有订阅者的队列。"""

    def __init__(self, server: FakeRedis) -> None:
        self._server = server
        self._channels: set[str] = set()
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._server._subscribers.setdefault(channel, set()).add(self)

    async def unsubscribe(self, channel: str) -> None:
        self._channels.discard(channel)
        subscribers = self._server._subscribers.get(channel)
        if subscribers is not None:
            subscribers.discard(self)

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = None
    ) -> dict[str, Any] | None:
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            # redis-py 的真实行为：超时返回 None，不抛异常
            return None

    def deliver(self, channel: str, message: str) -> None:
        if channel in self._channels:
            self._queue.put_nowait({"type": "message", "channel": channel, "data": message})

    async def aclose(self) -> None:
        for channel in list(self._channels):
            await self.unsubscribe(channel)
        self.closed = True


class FakePipeline:
    def __init__(self, server: FakeRedis) -> None:
        self._server = server
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def delete(self, key: str) -> FakePipeline:
        self._ops.append(("delete", (key,), {}))
        return self

    def hset(self, key: str, *args: Any, **kwargs: Any) -> FakePipeline:
        self._ops.append(("hset", (key, *args), kwargs))
        return self

    def expire(self, key: str, ttl: int) -> FakePipeline:
        self._ops.append(("expire", (key, ttl), {}))
        return self

    async def execute(self) -> list[Any]:
        results = []
        for name, args, kwargs in self._ops:
            results.append(await getattr(self._server, name)(*args, **kwargs))
        self._ops.clear()
        return results


class FakeRedis:
    """够用的内存 Redis。字符串、哈希、列表三套命名空间互不干扰。"""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: list[tuple[str, int]] = []
        self.published: list[tuple[str, str]] = []
        self.closed = False
        self._subscribers: dict[str, set[FakePubSub]] = {}

    # ---- 字符串 ----
    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.strings[key] = value
        if ex is not None:
            self.expires.append((key, ex))
        return True

    async def exists(self, key: str) -> int:
        return int(key in self.strings or key in self.hashes or key in self.lists)

    async def delete(self, key: str) -> int:
        hit = 0
        for store in (self.strings, self.hashes, self.lists):
            if key in store:
                del store[key]
                hit = 1
        return hit

    # ---- 哈希 ----
    async def hset(
        self,
        key: str,
        field: str | None = None,
        value: str | None = None,
        mapping: dict | None = None,
    ) -> int:
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)
        if field is None:
            return 0
        bucket[str(field)] = str(value)
        return 1

    async def hget(self, key: str, field: str) -> str | None:
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    # ---- 列表 ----
    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        bucket = self.lists.get(key, [])
        if end == -1:
            end = len(bucket) - 1
        if start < 0:
            start = max(0, len(bucket) + start)
        return bucket[start : end + 1]

    # ---- 其它 ----
    async def expire(self, key: str, ttl: int) -> bool:
        self.expires.append((key, ttl))
        return True

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        for subscriber in list(self._subscribers.get(channel, ())):
            subscriber.deliver(channel, message)
        return len(self._subscribers.get(channel, ()))

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def aclose(self) -> None:
        self.closed = True

    def expires_for(self, key: str) -> int:
        """某条 key 被续期过几次——用来证明「长回答不会中途过期」。"""
        return sum(1 for candidate, _ in self.expires if candidate == key)
