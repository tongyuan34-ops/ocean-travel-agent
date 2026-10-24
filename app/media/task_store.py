"""媒体任务元数据存储：Redis 或空实现。

任务本身只在内存里跑，但**元数据**必须跨进程重启保留，
否则历史会话里的图片卡片会查不到状态。图片本体由 MediaService
在任务完成时主动落到 media/，两者配合才能真正恢复历史图片。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import Settings

logger = logging.getLogger(__name__)


class NullMediaTaskStore:
    """内存后端时使用：不持久化，行为与改动前一致。"""

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return None

    async def save(self, task_id: str, data: dict[str, Any]) -> None:
        return None

    async def delete(self, task_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class RedisMediaTaskStore:
    def __init__(self, client, ttl_sec: int = 3600, prefix: str = "media:task:") -> None:
        self._client = client
        self._ttl = ttl_sec
        self._prefix = prefix

    def _key(self, task_id: str) -> str:
        return f"{self._prefix}{task_id}"

    async def get(self, task_id: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(self._key(task_id))
        except Exception:  # noqa: BLE001 - 存储故障不应打断查询
            logger.warning("读取媒体任务失败：%s", task_id, exc_info=True)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def save(self, task_id: str, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        try:
            if self._ttl > 0:
                await self._client.set(self._key(task_id), payload, ex=self._ttl)
            else:
                await self._client.set(self._key(task_id), payload)
        except Exception:  # noqa: BLE001 - 落库失败不影响本次任务
            logger.warning("写入媒体任务失败：%s", task_id, exc_info=True)

    async def delete(self, task_id: str) -> None:
        try:
            await self._client.delete(self._key(task_id))
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


def build_media_task_store(settings: Settings):
    """按 STORAGE_BACKEND 选择；Redis 连不上时降级为空实现。"""
    if settings.storage_backend != "redis":
        return NullMediaTaskStore()
    try:
        from redis.asyncio import from_url

        client = from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
        logger.info("媒体任务存储：Redis（TTL=%ss）", settings.media_task_ttl_sec)
        return RedisMediaTaskStore(client, ttl_sec=settings.media_task_ttl_sec)
    except Exception:  # noqa: BLE001 - 存储不可用时退回内存
        logger.warning("媒体任务存储初始化失败，降级为不持久化", exc_info=True)
        return NullMediaTaskStore()
