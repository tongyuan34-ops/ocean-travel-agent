"""Redis 会话存储。

会话以 JSON 存在单个 key 上：`chat:session:{session_id}`。
设置 TTL 时会话会随每次保存滑动续期，避免冷会话永久堆积。

选型说明：会话数据量小（最近 20 轮 + 一段摘要），单 key 读写足够；
不拆成 Hash 是因为整体读写天然原子，避免并发下字段错位。
"""

from __future__ import annotations

import json

from app.storage.base import SessionStore
from app.storage.models import ChatSession


class RedisSessionStore(SessionStore):
    def __init__(self, client, ttl_sec: int = 0, prefix: str = "chat:session:") -> None:
        self._client = client
        self._ttl = ttl_sec
        self._prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    async def create(self, session: ChatSession) -> None:
        await self.save(session)

    async def get(self, session_id: str) -> ChatSession | None:
        raw = await self._client.get(self._key(session_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # 数据损坏时当作不存在，避免把异常抛到业务层
            return None
        if not isinstance(data, dict):
            return None
        return ChatSession.from_dict(data)

    async def save(self, session: ChatSession) -> None:
        payload = json.dumps(session.to_dict(), ensure_ascii=False)
        if self._ttl > 0:
            await self._client.set(self._key(session.id), payload, ex=self._ttl)
        else:
            await self._client.set(self._key(session.id), payload)

    async def delete(self, session_id: str) -> None:
        await self._client.delete(self._key(session_id))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        try:
            await self._client.ping()
        except Exception:  # noqa: BLE001 - 自检不该把启动打崩
            return False
        return True
