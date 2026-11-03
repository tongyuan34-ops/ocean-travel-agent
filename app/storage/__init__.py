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
        from app.core.redis import async_redis
        from app.storage.redis_store import RedisSessionStore

        client = async_redis(settings.redis_url)
        logger.info("会话存储：Redis（%s，TTL=%ss）", settings.redis_url, settings.session_ttl_sec)
        return RedisSessionStore(client, ttl_sec=settings.session_ttl_sec)

    if settings.storage_backend == "file":
        from app.core.config import PROJECT_ROOT
        from app.storage.file import FileSessionStore

        # 桌面版默认走这条：单用户单进程，一个 JSON 文件就够，
        # 卸载时删掉 exe 所在目录即可，不留后门
        return FileSessionStore(PROJECT_ROOT / "data" / "sessions.json")

    logger.info("会话存储：内存（进程重启后会话会丢失）")
    return MemorySessionStore()
