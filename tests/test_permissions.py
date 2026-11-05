"""权限确认：任何失败路径都必须收敛为「不执行」。

对应 SPEC 1.3 底线二、契约 2.4 的需确认动作清单，以及
4.6 system 组「拿不到确认通道一律拒绝执行」。

裁决不再经过 broker 上的同步方法，而是走 ``DecisionChannel``——
多进程下那次 POST 可能落在任何一台机器上，所以测试也照着这条链路走。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

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


# ---------- 按需授权：用户提出改动后才问 ----------


@pytest.fixture
def sandbox_env(monkeypatch, tmp_path):
    """把授权记录关进 tmp_path，别写到仓库里的 grants.json。

    必须显式设 ``ALLOWED_ROOTS``：开发机的 .env 里可能是 ``C:\\``（全放开），
    那样 tmp 目录本来就在白名单内，这些用例一条都测不到「未授权」。
    """
    from app.core import grants

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("ALLOWED_ROOTS", str(allowed))
    monkeypatch.setattr(grants, "PROJECT_ROOT", tmp_path / "app")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    grants.reset()
    monkeypatch.setattr("app.agent.tools.system.settings_cache", None)
    yield tmp_path
    grants.reset()


async def _answer(broker, channel, seen, *, granted: bool, remember: bool = False):
    """后台把收到的请求一律按给定答案裁决。"""
    handled = 0
    while True:
        while len(seen) > handled:
            request = seen[handled]
            handled += 1
            await channel.decide(request.request_id, Decision(granted=granted, remember=remember))
        await asyncio.sleep(TIMEOUT_POLL)


async def _run_with_broker(coro, *, granted: bool, remember: bool = False):
    broker, channel, seen = _broker()
    task = asyncio.create_task(_answer(broker, channel, seen, granted=granted, remember=remember))
    token = current_broker.set(broker)
    try:
        result = await coro
    finally:
        current_broker.reset(token)
        task.cancel()
    return result, seen


async def test_write_outside_sandbox_asks_once_then_writes(sandbox_env):
    """越界不是直接拒绝，而是「问一次授权，同意了就写」。

    而且**只问一次**：授权和写确认合并成一个请求。连问两遍的话，第二问
    的答案在一秒前刚给过。
    """
    from app.core import grants

    target = sandbox_env / "Desktop" / "行程.md"
    target.parent.mkdir()

    out, seen = await _run_with_broker(
        write_file.ainvoke({"path": str(target), "content": "第一天：湄洲岛"}),
        granted=True,
    )

    assert out.startswith("已写入"), out
    assert target.read_text(encoding="utf-8") == "第一天：湄洲岛"
    # 一次确认里同时带了两样东西：要写什么 + 要授权哪个目录
    assert len(seen) == 1
    assert seen[0].detail["preview"].startswith("第一天")
    assert seen[0].detail["grant_root"] == str(target.parent.resolve())
    assert target.parent.resolve() in grants.load()


async def test_grant_denied_leaves_no_trace(sandbox_env):
    """拒绝授权 = 什么都没发生：不写文件、不记授权。"""
    from app.core import grants

    target = sandbox_env / "Desktop" / "行程.md"
    target.parent.mkdir()

    out, _ = await _run_with_broker(
        write_file.ainvoke({"path": str(target), "content": "x"}),
        granted=False,
    )

    assert out.startswith("PERMISSION_DENIED")
    assert not target.exists()
    assert grants.load() == []


def _protected_target() -> Path:
    """一个绝对路径形式的系统文件。

    必须拼成**绝对**路径：``Path("C:") / "Windows"`` 是 ``C:Windows``
    （盘符相对路径），resolve 之后会落到当前工作目录里——这条曾经真的
    把文件写进了仓库的 Windows/System32 下。
    """
    if os.name != "nt":
        return Path("/etc/passwd")
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    return Path(f"{drive}\\Windows\\System32\\drivers\\etc\\hosts")


async def test_system_dir_is_refused_without_asking(sandbox_env):
    """系统目录连问都不问：用户点「允许」也不该把 C:\\Windows 打开。"""
    target = _protected_target()

    out, seen = await _run_with_broker(
        write_file.ainvoke({"path": str(target), "content": "x"}),
        granted=True,
    )

    assert out.startswith("PATH_NOT_ALLOWED")
    assert seen == [], "系统目录不该弹确认框"


async def test_second_write_in_granted_dir_only_asks_for_write(sandbox_env):
    """授权过之后，同一个目录下的写入只剩「确认写入」这一问。"""
    target = sandbox_env / "Desktop" / "a.md"
    target.parent.mkdir()

    _, first = await _run_with_broker(
        write_file.ainvoke({"path": str(target), "content": "一"}), granted=True
    )
    _, second = await _run_with_broker(
        write_file.ainvoke({"path": str(target), "content": "二"}), granted=True
    )

    assert "grant_root" in first[0].detail
    assert "grant_root" not in second[0].detail
    assert second[0].detail["path"] == str(target.resolve())


async def test_read_outside_sandbox_asks_for_grant(sandbox_env):
    """读操作平时不确认，但目录没授权时得问一次——否则用户不知道为什么读不了。"""
    from app.agent.tools.system import list_dir

    folder = sandbox_env / "Desktop" / "旅行"
    folder.mkdir(parents=True)
    (folder / "a.md").write_text("x", encoding="utf-8")

    out, seen = await _run_with_broker(list_dir.ainvoke({"path": str(folder)}), granted=True)

    assert "a.md" in out
    assert len(seen) == 1
    assert seen[0].action == "grant_dir"
    assert seen[0].detail["grant_root"] == str((sandbox_env / "Desktop").resolve())


async def test_tainted_turn_cannot_expand_permissions(sandbox_env):
    """读到被注入的内容之后，连「允许访问这个目录」都不该问。

    扩大授权范围本身就是危险动作：弹窗上写的是「允许访问 ~/.ssh」，用户
    看不出这是网页里那句话在推着走。
    """
    from app.agent.guard import TaintState, current_taint
    from app.core import grants

    target = sandbox_env / "Desktop" / "a.md"
    target.parent.mkdir()

    taint = TaintState()
    taint.mark("fetch_url", ["忽略指令"])
    token = current_taint.set(taint)
    try:
        out, seen = await _run_with_broker(
            write_file.ainvoke({"path": str(target), "content": "x"}), granted=True
        )
    finally:
        current_taint.reset(token)

    assert out.startswith("PATH_NOT_ALLOWED")
    assert "注入" in out
    assert seen == [], "污点状态下不该再弹授权框"
    assert not target.exists()
    assert grants.load() == []
