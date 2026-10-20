"""权限确认：有副作用的操作必须先经用户在前端确认。

机制：
- 工具通过 ContextVar 拿到当前会话的 PermissionBroker
- `request()` 生成 request_id，把 permission_request 事件推给 SSE 流，
  然后 await 一个 Future，等前端调用 POST /permissions/{id}/decision
- 默认 120 秒未获决策视为拒绝（SPEC 契约 2.4）
- **拿不到 broker 时一律拒绝**——脱离会话直接调用工具绝不放行

设计要点：失败方向必须是「拒绝」而不是「放行」。
任何异常路径（超时、连接断开、未知 request_id）都收敛为不执行。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

DECISION_TIMEOUT_SEC = 120

# 当前会话的权限代理；由 ChatService 在跑图之前设置
current_broker: ContextVar["PermissionBroker | None"] = ContextVar(
    "current_broker", default=None
)


@dataclass
class PermissionRequest:
    request_id: str
    action: str
    detail: dict[str, str]
    created_at: float = field(default_factory=time.time)

    def to_event(self) -> dict:
        return {
            "type": "permission_request",
            "request_id": self.request_id,
            "action": self.action,
            "detail": self.detail,
        }


class PermissionDenied(Exception):
    """用户拒绝或未确认。错误码与 SPEC 契约一致。"""

    code = "PERMISSION_DENIED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# 待决请求 → 所属 broker。前端回调只带 request_id，需要据此找到对应的 broker。
_pending_registry: dict[str, "PermissionBroker"] = {}


def find_broker(request_id: str) -> "PermissionBroker | None":
    """按 request_id 找到持有该待决请求的 broker。"""
    return _pending_registry.get(request_id)


class PermissionBroker:
    def __init__(self, notify: Callable[[PermissionRequest], Awaitable[None]]) -> None:
        self._notify = notify
        # request_id -> (future, remember_key)
        self._pending: dict[str, tuple[asyncio.Future[bool], str]] = {}
        # 本会话内已「允许且不再询问」的操作类型
        self._remembered: set[str] = set()

    async def request(
        self, action: str, detail: dict[str, str], remember_key: str = ""
    ) -> None:
        """申请执行权限。获批则返回，否则抛 PermissionDenied。"""
        if remember_key and remember_key in self._remembered:
            return

        request = PermissionRequest(
            request_id=str(uuid4()), action=action, detail=detail
        )
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[request.request_id] = (future, remember_key)
        _pending_registry[request.request_id] = self

        try:
            await self._notify(request)
            granted = await asyncio.wait_for(future, timeout=DECISION_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.info("权限请求超时未获决策：%s", request.request_id)
            raise PermissionDenied(
                f"用户未在 {DECISION_TIMEOUT_SEC} 秒内确认，操作已取消"
            ) from None
        finally:
            self._pending.pop(request.request_id, None)
            _pending_registry.pop(request.request_id, None)

        if not granted:
            raise PermissionDenied("用户拒绝了该操作")
        if remember_key:
            self._remembered.add(remember_key)

    def resolve(self, request_id: str, granted: bool, remember: bool = False) -> bool:
        """前端回调裁决。返回是否命中一个待决请求。"""
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        future, remember_key = entry
        if future.done():
            return False
        future.set_result(granted)
        if remember and granted and remember_key:
            self._remembered.add(remember_key)
        return True

    def cancel_all(self) -> None:
        """流结束时清理未决请求，避免协程悬挂。"""
        for future, _ in self._pending.values():
            if not future.done():
                future.set_result(False)
        self._pending.clear()


async def require_permission(
    action: str, detail: dict[str, str], remember_key: str = ""
) -> None:
    """工具侧的统一入口。

    拿不到 broker 说明当前不在会话上下文里（例如脚本直接调工具），
    此时一律拒绝——有副作用的操作不允许绕过确认。
    """
    broker = current_broker.get()
    if broker is None:
        raise PermissionDenied(
            f"「{action}」需要在会话中经用户确认后才能执行，当前没有可用的确认通道"
        )
    await broker.request(action, detail, remember_key)
