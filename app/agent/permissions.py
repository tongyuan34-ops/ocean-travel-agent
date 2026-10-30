"""权限确认：有副作用的操作必须先经用户在前端确认。

机制：
- 工具通过 ContextVar 拿到当前会话的 PermissionBroker
- `request()` 生成 request_id，把 permission_request 事件推给 SSE 流，
  然后 await 一个裁决，等前端调用 POST /permissions/{id}/decision
- 默认 120 秒未获决策视为拒绝（SPEC 契约 2.4）
- **拿不到 broker 时一律拒绝**——脱离会话直接调用工具绝不放行

设计要点：失败方向必须是「拒绝」而不是「放行」。
任何异常路径（超时、连接断开、未知 request_id）都收敛为不执行。

## 为什么把「裁决通道」抽出来

原来待决请求是一个模块级 dict：`request_id -> broker`，裁决直接 set 一个
`asyncio.Future`。这在单进程里最省事，但 `uvicorn --workers > 1` 下
**前端那次 POST 大概率落到另一个进程**——那个进程的 dict 里没有这条请求，
于是 404，而真正在等的那台机器一直等到 120 秒超时，用户点了「允许」也没用。

所以拆成 `DecisionChannel`：

- `LocalDecisionChannel`：进程内 Future，零依赖；
- `RedisDecisionChannel`：key 存裁决结果 + Pub/Sub 唤醒等待方。

等待方先查 key 再订阅，覆盖「裁决比订阅先到」这个竞态；订阅只是让延迟变低，
真正的真相永远是 key。这样即使 Pub/Sub 丢消息，最多慢一秒，不会丢裁决。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

DECISION_TIMEOUT_SEC = 120

# 当前会话的权限代理；由 ChatService 在跑图之前设置
current_broker: ContextVar[PermissionBroker | None] = ContextVar("current_broker", default=None)


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


@dataclass(frozen=True)
class Decision:
    """前端的一次裁决。``remember`` 只对可记忆的操作类型有意义。"""

    granted: bool
    remember: bool = False

    def to_json(self) -> str:
        return json.dumps({"granted": self.granted, "remember": self.remember})

    @classmethod
    def from_json(cls, raw: str) -> Decision:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # 内容坏了按拒绝处理：这里宁可比用户更保守
            return cls(granted=False)
        if not isinstance(data, dict):
            return cls(granted=False)
        return cls(granted=bool(data.get("granted")), remember=bool(data.get("remember")))


class PermissionDenied(Exception):
    """用户拒绝或未确认。错误码与 SPEC 契约一致。"""

    code = "PERMISSION_DENIED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ==================== 裁决通道 ====================


class DecisionChannel(Protocol):
    """待决请求的登记与裁决。等待方和裁决方可能不在同一个进程里。"""

    async def open(self, request_id: str) -> None: ...

    async def wait(self, request_id: str, timeout: float) -> Decision | None:
        """等一个裁决；超时返回 None（调用方据此拒绝）。"""
        ...

    async def decide(self, request_id: str, decision: Decision) -> bool:
        """写入裁决。返回是否命中一个仍在等待的请求。"""
        ...

    async def close(self, request_id: str) -> None: ...

    async def aclose(self) -> None: ...


class LocalDecisionChannel:
    """进程内裁决通道。"""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Decision]] = {}

    async def open(self, request_id: str) -> None:
        self._pending[request_id] = asyncio.get_running_loop().create_future()

    async def wait(self, request_id: str, timeout: float) -> Decision | None:
        future = self._pending.get(request_id)
        if future is None:
            return None
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None

    async def decide(self, request_id: str, decision: Decision) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    async def close(self, request_id: str) -> None:
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            # 取消而不是留一个悬空 Future：等待方已经走了，没人会再读它
            future.cancel()

    async def aclose(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()


class RedisDecisionChannel:
    """跨进程裁决通道：key 存结果，Pub/Sub 通知。

    key 的三种状态：不存在（无此请求）、``pending``、裁决 JSON。
    等待方和裁决方都只依赖 key 的最终值，Pub/Sub 丢了最多慢一个轮询周期。
    """

    #: Pub/Sub 丢消息时的回读间隔
    POLL_SEC = 1.0

    def __init__(self, client: Any, prefix: str = "perm:") -> None:
        self._client = client
        self._prefix = prefix

    def _key(self, request_id: str) -> str:
        return f"{self._prefix}{request_id}"

    def _channel(self, request_id: str) -> str:
        return f"{self._prefix}{request_id}:decision"

    async def _read(self, request_id: str) -> Decision | None:
        raw = await self._client.get(self._key(request_id))
        if not raw or raw == "pending":
            return None
        return Decision.from_json(str(raw))

    async def open(self, request_id: str) -> None:
        await self._client.set(self._key(request_id), "pending", ex=DECISION_TIMEOUT_SEC + 30)

    async def wait(self, request_id: str, timeout: float) -> Decision | None:
        # 先查一次：裁决可能比我们订阅得更早到
        found = await self._read(request_id)
        if found is not None:
            return found

        pubsub = self._client.pubsub()
        try:
            await pubsub.subscribe(self._channel(request_id))
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return await self._read(request_id)
                # 不管有没有被唤醒都回读 key：消息会丢，key 不会。
                # 这里的超时是正常路径——这一秒确实没人裁决
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=min(remaining, self.POLL_SEC)
                        ),
                        timeout=min(remaining, self.POLL_SEC) + 1,
                    )
                found = await self._read(request_id)
                if found is not None:
                    return found
        finally:
            try:
                await pubsub.unsubscribe(self._channel(request_id))
                await pubsub.aclose()
            except Exception:
                logger.debug("关闭权限 pubsub 出错 | request=%s", request_id, exc_info=True)

    async def decide(self, request_id: str, decision: Decision) -> bool:
        key = self._key(request_id)
        if not await self._client.exists(key):
            return False
        await self._client.set(key, decision.to_json(), ex=DECISION_TIMEOUT_SEC + 30)
        await self._client.publish(self._channel(request_id), decision.to_json())
        return True

    async def close(self, request_id: str) -> None:
        await self._client.delete(self._key(request_id))

    async def aclose(self) -> None:
        await self._client.aclose()


# ==================== 会话级 broker ====================


class PermissionBroker:
    """一轮对话的权限代理：登记请求、等待裁决、记住「本次会话不再问」。"""

    def __init__(
        self,
        channel: DecisionChannel,
        notify: Callable[[PermissionRequest], Awaitable[None]],
    ) -> None:
        self._channel = channel
        self._notify = notify
        # 本会话内已「允许且不再询问」的操作类型
        self._remembered: set[str] = set()
        # 本 broker 自己的待决请求，用于流结束时清理
        self._outstanding: set[str] = set()

    async def request(self, action: str, detail: dict[str, str], remember_key: str = "") -> None:
        """申请执行权限。获批则返回，否则抛 PermissionDenied。"""
        if remember_key and remember_key in self._remembered:
            return

        request = PermissionRequest(request_id=str(uuid4()), action=action, detail=detail)
        self._outstanding.add(request.request_id)
        await self._channel.open(request.request_id)
        try:
            await self._notify(request)
            decision = await self._channel.wait(request.request_id, DECISION_TIMEOUT_SEC)
        finally:
            self._outstanding.discard(request.request_id)
            await self._channel.close(request.request_id)

        if decision is None:
            logger.info("权限请求超时未获决策：%s", request.request_id)
            raise PermissionDenied(f"用户未在 {DECISION_TIMEOUT_SEC} 秒内确认，操作已取消")
        if not decision.granted:
            raise PermissionDenied("用户拒绝了该操作")
        if decision.remember and remember_key:
            self._remembered.add(remember_key)

    async def cancel_all(self) -> None:
        """流结束时清理未决请求，避免协程悬挂。

        走 ``channel.decide`` 而不是直接改本地状态：跨进程时待决请求的
        Future 在生成端，清理也必须经过同一个通道。
        """
        for request_id in list(self._outstanding):
            await self._channel.decide(request_id, Decision(granted=False))
        self._outstanding.clear()


async def require_permission(action: str, detail: dict[str, str], remember_key: str = "") -> None:
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


def build_decision_channel(settings: Any) -> DecisionChannel:
    """按 ``SHARED_STATE`` 选择实现，规则与事件流一致（见 streams.build_event_log）。"""
    mode = (getattr(settings, "shared_state", "auto") or "auto").lower()
    use_redis = mode == "redis" or (mode == "auto" and settings.storage_backend == "redis")
    if not use_redis:
        logger.info("权限裁决通道：内存（单进程；--workers > 1 时回调会 404）")
        return LocalDecisionChannel()

    from app.core.redis import async_redis

    client = async_redis(settings.redis_url)
    logger.info("权限裁决通道：Redis（%s）", settings.redis_url)
    return RedisDecisionChannel(client)
