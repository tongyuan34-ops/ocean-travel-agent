"""存储层：会话存储的抽象与实现，以及按配置选择后端的工厂。"""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.storage.base import SessionStore
from app.storage.memory import MemorySessionStore
from app.storage.models import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

__all__ = [
    "ChatMessage",
    "ChatSession",
    "MemorySessionStore",
    "SessionStore",
    "build_session_store",
]


def build_session_store(settings: Settings) -> SessionStore:
    """按 .env 的 STORAGE_BACKEND 选择实现。

    redis 客户端延迟导入：用内存后端时无需安装 redis 包。
    """
    if settings.storage_backend == "redis":
        from redis.asyncio import from_url

        from app.storage.redis_store import RedisSessionStore

        client = from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        logger.info("会话存储：Redis（%s，TTL=%ss）", settings.redis_url, settings.session_ttl_sec)
        return RedisSessionStore(client, ttl_sec=settings.session_ttl_sec)

    logger.info("会话存储：内存（进程重启后会话会丢失）")
    return MemorySessionStore()
