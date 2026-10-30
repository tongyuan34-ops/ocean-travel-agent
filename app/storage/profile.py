"""用户长期偏好存储：按设备隔离，Redis 持久化或内存回退。

key 设计：
- `user:profile:{user_id}` → `{"items": [...], "updated_at": "..."}`，永不过期
- `user:suggest:{user_id}` → `{"items": [...]}`，待用户确认的候选，1 小时自动过期

user_id 由前端生成的设备 ID 提供（`X-Device-ID` 头），缺失时统一落到 `default`。
"""

from __future__ import annotations

import abc
import asyncio
import json
from typing import Any

from app.core.config import Settings

# 候选偏好保留时间：过久未确认就丢弃，避免下次打开时突然弹出一堆旧建议
SUGGEST_TTL_SEC = 3600


class ProfileStore(abc.ABC):
    """偏好存储接口。Redis 与内存实现必须行为一致。"""

    @abc.abstractmethod
    async def get_profile(self, user_id: str) -> dict[str, Any] | None:
        """读取偏好；不存在返回 None。"""

    @abc.abstractmethod
    async def save_profile(self, user_id: str, data: dict[str, Any]) -> None:
        """整体写回偏好。"""

    @abc.abstractmethod
    async def delete_profile(self, user_id: str) -> None:
        """清空该用户的偏好。"""

    @abc.abstractmethod
    async def get_suggestions(self, user_id: str) -> dict[str, Any] | None:
        """读取待确认候选。"""

    @abc.abstractmethod
    async def save_suggestions(self, user_id: str, data: dict[str, Any]) -> None:
        """写入待确认候选（带过期）。"""

    @abc.abstractmethod
    async def delete_suggestions(self, user_id: str) -> None:
        """丢弃候选。"""

    async def aclose(self) -> None:
        """释放连接。默认无操作——内存实现无需处理，Redis 实现自行覆盖。"""
        return None


def _copy(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return json.loads(json.dumps(data, ensure_ascii=False))


class MemoryProfileStore(ProfileStore):
    """进程内存实现：仅开发与测试使用，重启即丢。"""

    def __init__(self) -> None:
        self._profiles: dict[str, dict[str, Any]] = {}
        self._suggestions: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_profile(self, user_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return _copy(self._profiles.get(user_id))

    async def save_profile(self, user_id: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._profiles[user_id] = _copy(data) or {}

    async def delete_profile(self, user_id: str) -> None:
        async with self._lock:
            self._profiles.pop(user_id, None)

    async def get_suggestions(self, user_id: str) -> dict[str, Any] | None:
        async with self._lock:
            return _copy(self._suggestions.get(user_id))

    async def save_suggestions(self, user_id: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._suggestions[user_id] = _copy(data) or {}

    async def delete_suggestions(self, user_id: str) -> None:
        async with self._lock:
            self._suggestions.pop(user_id, None)


class RedisProfileStore(ProfileStore):
    """Redis 实现：偏好永久保存，候选带 TTL。"""

    def __init__(
        self, client, prefix: str = "user:profile:", suggest_prefix: str = "user:suggest:"
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._suggest_prefix = suggest_prefix

    def _key(self, user_id: str) -> str:
        return f"{self._prefix}{user_id}"

    def _suggest_key(self, user_id: str) -> str:
        return f"{self._suggest_prefix}{user_id}"

    async def _load(self, key: str) -> dict[str, Any] | None:
        raw = await self._client.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def get_profile(self, user_id: str) -> dict[str, Any] | None:
        return await self._load(self._key(user_id))

    async def save_profile(self, user_id: str, data: dict[str, Any]) -> None:
        await self._client.set(self._key(user_id), json.dumps(data, ensure_ascii=False))

    async def delete_profile(self, user_id: str) -> None:
        await self._client.delete(self._key(user_id))

    async def get_suggestions(self, user_id: str) -> dict[str, Any] | None:
        return await self._load(self._suggest_key(user_id))

    async def save_suggestions(self, user_id: str, data: dict[str, Any]) -> None:
        await self._client.set(
            self._suggest_key(user_id),
            json.dumps(data, ensure_ascii=False),
            ex=SUGGEST_TTL_SEC,
        )

    async def delete_suggestions(self, user_id: str) -> None:
        await self._client.delete(self._suggest_key(user_id))

    async def aclose(self) -> None:
        await self._client.aclose()


def build_profile_store(settings: Settings) -> ProfileStore:
    """按 STORAGE_BACKEND 选择实现；Redis 客户端延迟导入。"""
    if settings.storage_backend == "redis":
        from app.core.redis import async_redis

        client = async_redis(settings.redis_url)
        return RedisProfileStore(client)
    return MemoryProfileStore()
