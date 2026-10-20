"""会话存储抽象。

SPEC 4.11：存储层抽象接口 + 内存实现，预留 Redis 实现位。

约定（两种实现必须一致）：
- `get` 返回的是调用方独占的副本，修改后必须显式 `save` 才会持久化
- 这样上层代码不用关心后端是内存还是 Redis
"""

from __future__ import annotations

import abc

from app.storage.models import ChatSession


class SessionStore(abc.ABC):
    """会话存储接口。"""

    @abc.abstractmethod
    async def create(self, session: ChatSession) -> None:
        """新建会话。"""

    @abc.abstractmethod
    async def get(self, session_id: str) -> ChatSession | None:
        """读取会话；不存在返回 None。"""

    @abc.abstractmethod
    async def save(self, session: ChatSession) -> None:
        """写回会话（新建与更新共用）。"""

    @abc.abstractmethod
    async def delete(self, session_id: str) -> None:
        """删除会话。"""

    @abc.abstractmethod
    async def aclose(self) -> None:
        """释放连接等资源。"""

    async def ping(self) -> bool:
        """连通性自检，供启动时告警使用。内存实现恒为可用。"""
        return True
