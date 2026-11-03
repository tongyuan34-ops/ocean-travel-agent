"""文件会话存储：桌面版的持久化后端。

为什么这些用例值得写：这个后端的失败方式**不报错**。
落盘失败只打日志、损坏文件会被改名、写入不是原子的话断电留半截 JSON——
三种情况在开发机上全都正常，只在用户关掉程序再打开时炸。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from app.storage.file import FileSessionStore
from app.storage.models import ChatMessage, ChatSession


def _message(role: str, content: str, index: int) -> ChatMessage:
    return ChatMessage(
        id=f"m{index}",
        created_at="2026-11-03T09:00:00",
        role=role,
        content=content,
    )


def _session(session_id: str = "s1") -> ChatSession:
    return ChatSession(
        id=session_id,
        created_at="2026-11-03T09:00:00",
        messages=[
            _message("user", "帮我排个湄洲岛两天", 1),
            _message("assistant", "第一天先去妈祖文化园。", 2),
        ],
    )


async def test_session_survives_restart(tmp_path: Path):
    """关掉程序再打开，聊天记录还在——桌面版最基本的承诺。"""
    path = tmp_path / "data" / "sessions.json"
    first = FileSessionStore(path)
    await first.create(_session())

    second = FileSessionStore(path)
    loaded = await second.get("s1")

    assert loaded is not None
    assert [m.role for m in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[0].content == "帮我排个湄洲岛两天"


async def test_save_is_visible_to_next_process(tmp_path: Path):
    """save 之后必须已经落盘，不能等到进程退出才写——用户是直接关窗口的。"""
    path = tmp_path / "sessions.json"
    store = FileSessionStore(path)
    session = _session()
    await store.create(session)

    session.messages.append(_message("user", "第二天呢", 3))
    await store.save(session)

    assert "第二天呢" in path.read_text(encoding="utf-8")


async def test_write_leaves_no_temp_file(tmp_path: Path):
    """先写 .tmp 再 os.replace：写完之后临时文件必须已经不在。

    留着的话，用户目录里会慢慢堆一堆 ``sessions.tmp``，
    卸载时看着像没清理干净。
    """
    path = tmp_path / "sessions.json"
    store = FileSessionStore(path)
    await store.create(_session())

    assert not path.with_suffix(".tmp").exists()
    assert path.exists()


async def test_file_carries_a_version_field(tmp_path: Path):
    """格式要能演进：没有 version 的话，以后改结构只能靠猜。"""
    path = tmp_path / "sessions.json"
    await FileSessionStore(path).create(_session())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["sessions"]) == 1


async def test_corrupt_file_is_kept_as_evidence(tmp_path: Path):
    """文件读坏了不能让程序起不来，但**不能删**——删了才是真丢数据。

    实际发生过的场景：用户手动编辑过、磁盘写满、同步盘冲突。
    这时应该改名留证，用空库继续跑，让用户至少还能用起来。
    """
    path = tmp_path / "sessions.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    store = FileSessionStore(path)  # 不能抛

    assert await store.get("s1") is None
    assert path.with_suffix(".broken.json").exists()
    assert "这不是 JSON" in path.with_suffix(".broken.json").read_text(encoding="utf-8")


async def test_one_bad_record_does_not_drop_the_rest(tmp_path: Path):
    """单条记录解析失败只跳过它，不能让整个会话库变成空的。"""
    path = tmp_path / "sessions.json"
    good = _session("ok").to_dict()
    # summarized_count 存成了字符串：int() 会抛 ValueError，这条必须被跳过
    path.write_text(
        json.dumps({"version": 1, "sessions": [{"id": "bad", "summarized_count": "abc"}, good]}),
        encoding="utf-8",
    )

    store = FileSessionStore(path)

    assert await store.get("ok") is not None
    assert await store.get("bad") is None


async def test_unwritable_target_does_not_break_the_turn(tmp_path: Path):
    """落盘失败只记日志，这一轮对话照常进行。

    内存里还有一份，用户这次能继续用；下次启动会丢，但总比当场报错强。
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")

    store = FileSessionStore(blocker / "sessions.json")  # 父目录建不出来
    await store.create(_session())

    assert await store.get("s1") is not None


async def test_delete_is_persisted(tmp_path: Path):
    """删了之后重开也不能复活——否则用户会看到「删不掉的会话」。"""
    path = tmp_path / "sessions.json"
    store = FileSessionStore(path)
    await store.create(_session())
    await store.delete("s1")

    assert await FileSessionStore(path).get("s1") is None


# ---------- 落盘成本：只重写变过的、不阻塞事件循环 ----------


async def test_saving_one_session_does_not_reserialize_the_others(tmp_path: Path, monkeypatch):
    """改一个会话不该把其余几百个重新 to_dict + dumps。

    实测 200 会话 × 50 条时这两步占 26ms / 33ms，而每条消息都要付一次。
    """
    path = tmp_path / "sessions.json"
    store = FileSessionStore(path)
    for index in range(5):
        await store.create(_session(f"s{index}"))

    session = await store.get("s0")
    assert session is not None

    calls: list[str] = []
    original = ChatSession.to_dict

    def counting(self) -> dict:
        calls.append(self.id)
        return original(self)

    monkeypatch.setattr(ChatSession, "to_dict", counting)

    session.messages.append(_message("user", "再问一句", 3))
    await store.save(session)

    # 只有 s0 被重新序列化（save 的深拷贝一次、渲染一次）
    assert set(calls) == {"s0"}
    assert "再问一句" in path.read_text(encoding="utf-8")


async def test_concurrent_saves_are_coalesced(tmp_path: Path, monkeypatch):
    """同时到达的多次 save 不该各写一遍盘，但每个都要真的落盘。"""
    path = tmp_path / "sessions.json"
    store = FileSessionStore(path)
    await store.create(_session("s0"))

    writes: list[str] = []
    original = store._write_sync

    def slow_write(text: str) -> bool:
        # 写慢一点，让后面的 save 有时间堆在锁上
        time.sleep(0.05)
        writes.append(text)
        return original(text)

    monkeypatch.setattr(store, "_write_sync", slow_write)

    copies = []
    for _ in range(5):
        found = await store.get("s0")
        assert found is not None
        copies.append(found)
    await asyncio.gather(*(store.save(item) for item in copies))

    assert 1 <= len(writes) < 5
    # 最后落盘的内容必须是最新的
    assert "第二天呢" not in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").count('"id": "s0"') == 1


async def test_slow_disk_does_not_freeze_the_event_loop(tmp_path: Path, monkeypatch):
    """落盘在慢盘上跑几百毫秒时，事件循环必须还在转。

    原来是同步 ``write_text`` 跑在事件循环里，每追加一条消息就阻塞一次，
    期间 SSE 心跳和别的请求全停。
    """
    store = FileSessionStore(tmp_path / "s.json")
    await store.create(_session("s0"))

    original = store._write_sync

    def slow_write(text: str) -> bool:
        time.sleep(0.3)
        return original(text)

    monkeypatch.setattr(store, "_write_sync", slow_write)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        session = await store.get("s0")
        assert session is not None
        await store.save(session)
    finally:
        task.cancel()

    # 0.3 秒的慢写期间，10ms 一跳的计时器至少该跳十几次；
    # 同步写盘的话这个数会是 0
    assert ticks >= 10
