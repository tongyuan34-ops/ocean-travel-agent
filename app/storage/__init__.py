"""存储层：会话存储的抽象与实现，以及按配置选择后端的工厂。"""

from __future__ import annotations

import logging
from pathlib import Path

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

    if settings.storage_backend in {"sqlite", "sqlite3"}:
        from app.core.config import PROJECT_ROOT
        from app.storage.sqlite import SQLiteSessionStore

        # 多租户/多人共用的默认：写入只碰一行、列表走 tenant 索引，
        # 而且不用像 file 后端那样启动时把全部会话解析进内存
        path = PROJECT_ROOT / "data" / "sessions.db"
        legacy = PROJECT_ROOT / "data" / "sessions.json"
        if not path.exists() and legacy.exists():
            # 首次切到 SQLite：自动把旧 JSON 灌进来。
            # 桌面版升级就是这个场景——让用户为了升级手动跑脚本不现实，
            # 而「历史会话突然全没了」是最容易被当成数据丢失的故障
            count = import_legacy_sessions(legacy, path)
            logger.info("已把 %s 的 %d 条历史会话导入 SQLite（%s）", legacy, count, path)
        return SQLiteSessionStore(path)

    logger.info("会话存储：内存（进程重启后会话会丢失）")
    return MemorySessionStore()


def import_legacy_sessions(source: Path, target: Path) -> int:
    """把旧版 ``sessions.json`` 一次性导入 SQLite，返回条数。

    同步执行：它在启动路径上，而且只是一次性动作，不值得为它开事件循环。
    读文件复用 ``file.read_file``，坏文件处理与 file 后端完全一致。
    """
    from app.storage.file import read_file
    from app.storage.sqlite import SQLiteSessionStore

    sessions = read_file(source)
    store = SQLiteSessionStore(target)
    try:
        return store.bulk_upsert(sessions)
    finally:
        store.close()
