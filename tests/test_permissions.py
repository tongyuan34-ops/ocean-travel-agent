"""权限确认：任何失败路径都必须收敛为「不执行」。

对应 SPEC 1.3 底线二、契约 2.4 的需确认动作清单，以及
4.6 system 组「拿不到确认通道一律拒绝执行」。

裁决不再经过 broker 上的同步方法，而是走 ``DecisionChannel``——
多进程下那次 POST 可能落在任何一台机器上，所以测试也照着这条链路走。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent import permissions as perm_module
from app.agent.permissions import (
    Decision,
    LocalDecisionChannel,
    PermissionBroker,
    PermissionDenied,
    current_broker,
    require_permission,
)
from app.agent.tools.system import write_file

TIMEOUT_POLL = 0.01


def _broker() -> tuple[PermissionBroker, LocalDecisionChannel, list]:
    """造一对 broker / channel，并把收到的通知收集起来。"""
    seen: list = []

    async def notify(request) -> None:
        seen.append(request)

    channel = LocalDecisionChannel()
    return PermissionBroker(channel, notify), channel, seen


async def _start(broker: PermissionBroker, seen: list, **kwargs) -> asyncio.Task:
    """发起一次权限申请，并等到通知已经发出。"""
    task = asyncio.create_task(broker.request(**kwargs))
    for _ in range(200):
        if seen:
            return task
        await asyncio.sleep(TIMEOUT_POLL)
    raise AssertionError("权限申请没有发出通知")


async def test_without_broker_refuses():
    """脱离会话直接调工具时，必须拒绝而不是放行。"""
    assert current_broker.get() is None
    with pytest.raises(PermissionDenied):
        await require_permission("write_file", {"path": "x"})


async def test_user_denial_does_not_execute():
    broker, channel, seen = _broker()
    task = await _start(broker, seen, action="write_file", detail={"path": "x"}, remember_key="k")

    assert not task.done()
    assert await channel.decide(seen[0].request_id, Decision(granted=False)) is True
    with pytest.raises(PermissionDenied):
        await task


async def test_grant_returns():
    broker, channel, seen = _broker()
    task = await _start(broker, seen, action="delete_path", detail={"path": "x"})
    assert await channel.decide(seen[0].request_id, Decision(granted=True)) is True
    await task  # 不抛异常即通过


async def test_remember_skips_second_prompt():
    broker, channel, seen = _broker()
    task = await _start(broker, seen, action="write_file", detail={}, remember_key="same")
    await channel.decide(seen[0].request_id, Decision(granted=True, remember=True))
    await task

    # 第二次同类请求不再通知用户，直接放行
    await broker.request(action="write_file", detail={}, remember_key="same")
    assert len(seen) == 1


async def test_timeout_is_treated_as_denial(monkeypatch):
    monkeypatch.setattr(perm_module, "DECISION_TIMEOUT_SEC", 0.05)
    broker, _channel, _seen = _broker()
    with pytest.raises(PermissionDenied):
        await broker.request(action="set_wallpaper", detail={})


async def test_unknown_request_id_not_resolved():
    _broker_obj, channel, _seen = _broker()
    assert await channel.decide("not-a-real-id", Decision(granted=True)) is False


async def test_cancel_all_denies_pending():
    broker, _channel, seen = _broker()
    task = await _start(broker, seen, action="delete_path", detail={})
    await broker.cancel_all()
    with pytest.raises(PermissionDenied):
        await task


async def test_decision_survives_a_lost_wakeup():
    """Pub/Sub 丢了消息也不能丢裁决：等待方回读 key 就能拿到。"""
    channel = LocalDecisionChannel()
    await channel.open("req-1")
    # 裁决先到，等待方后到——这就是「消息比订阅先到」那个竞态
    assert await channel.decide("req-1", Decision(granted=True)) is True
    decision = await channel.wait("req-1", timeout=0.5)
    assert decision is not None and decision.granted is True


# ---------- 工具层：拒绝后绝不落盘 ----------


async def test_write_file_refuses_without_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setattr("app.agent.tools.system.settings_cache", None)

    out = await write_file.ainvoke({"path": str(tmp_path / "x.txt"), "content": "hi"})
    assert out.startswith("PERMISSION_DENIED")
    assert not (tmp_path / "x.txt").exists()


async def test_write_file_outside_sandbox_rejected_before_permission(monkeypatch, tmp_path):
    """路径越界要在「要权限之前」就被拦下，不能先弹确认框再拒绝。"""
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setenv("ALLOWED_ROOTS", str(root))
    monkeypatch.setattr("app.agent.tools.system.settings_cache", None)

    out = await write_file.ainvoke({"path": str(tmp_path / "outside.txt"), "content": "hi"})
    assert out.startswith("PATH_NOT_ALLOWED")
    assert not (tmp_path / "outside.txt").exists()
