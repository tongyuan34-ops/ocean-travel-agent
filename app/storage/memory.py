"""内存会话存储：进程重启即丢失，仅用于开发与测试。"""

from __future__ import annotations

import asyncio

from app.storage.base import SessionStore
from app.storage.models import ChatSession


def _copy(session: ChatSession) -> ChatSession:
    """深拷贝：保证调用方拿到的是副本，与 Redis 实现行为一致。"""
    return ChatSession.from_dict(session.to_dict())


class MemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._data: dict[str, ChatSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, session: ChatSession) -> None:
        async with self._lock:
            self._data[session.id] = _copy(session)

    async def get(self, session_id: str) -> ChatSession | None:
        async with self._lock:
            found = self._data.get(session_id)
            return _copy(found) if found is not None else None

    async def save(self, session: ChatSession) -> None:
        async with self._lock:
            self._data[session.id] = _copy(session)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)

    async def aclose(self) -> None:
        return None
