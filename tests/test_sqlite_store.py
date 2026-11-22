"""SQLite 会话存储：多租户下的持久化后端。

和文件后端一样，这里的失败方式很安静：写失败只记日志、坏记录只跳过。
所以用例重点覆盖「重开进程还在」「删除不复活」「按租户过滤不串数据」
以及迁移脚本不丢会话。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from app.storage import import_legacy_sessions
from app.storage.models import ChatMessage, ChatSession
from app.storage.sqlite import SQLiteSessionStore


def _message(role: str, content: str, index: int) -> ChatMessage:
    return ChatMessage(id=f"m{index}", created_at="2026-11-16T09:00:00", role=role, content=content)


def _session(session_id: str = "s1", tenant: str = "default") -> ChatSession:
    return ChatSession(
        id=session_id,
        created_at="2026-11-16T09:00:00",
        tenant=tenant,
        messages=[
            _message("user", "帮我排个湄洲岛两天", 1),
            _message("assistant", "第一天先去妈祖文化园。", 2),
        ],
    )


async def test_session_survives_restart(tmp_path: Path):
    path = tmp_path / "data" / "sessions.db"
    first = SQLiteSessionStore(path)
    await first.create(_session())
    await first.aclose()

    second = SQLiteSessionStore(path)
    loaded = await second.get("s1")
    await second.aclose()

    assert loaded is not None
    assert loaded.tenant == "default"
    assert [m.role for m in loaded.messages] == ["user", "assistant"]


async def test_save_replaces_only_that_session(tmp_path: Path):
    """追加一条消息后，改的只是这一行，其余会话不受影响。"""
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.create(_session("s1"))
    await store.create(_session("s2"))

    session = await store.get("s1")
    assert session is not None
    session.messages.append(_message("user", "第二天呢", 3))
    await store.save(session)

    reopened = SQLiteSessionStore(tmp_path / "s.db")
    updated = await reopened.get("s1")
    other = await reopened.get("s2")
    assert updated is not None and other is not None
    assert updated.messages[-1].content == "第二天呢"
    assert len(other.messages) == 2
    await reopened.aclose()


async def test_delete_is_persisted(tmp_path: Path):
    path = tmp_path / "s.db"
    store = SQLiteSessionStore(path)
    await store.create(_session())
    await store.delete("s1")
    await store.aclose()

    assert await SQLiteSessionStore(path).get("s1") is None


async def test_list_filters_by_tenant(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.create(_session("a1", "acme"))
    await store.create(_session("a2", "acme"))
    await store.create(_session("g1", "globex"))

    assert sorted(item.id for item in await store.list("acme")) == ["a1", "a2"]
    assert [item.id for item in await store.list("globex")] == ["g1"]
    assert len(await store.list()) == 3
    assert await store.list("nobody") == []


async def test_get_returns_an_independent_copy(tmp_path: Path):
    """改动拿到的对象不会悄悄写进库——必须显式 save。"""
    store = SQLiteSessionStore(tmp_path / "s.db")
    await store.create(_session())

    got = await store.get("s1")
    assert got is not None
    got.messages.clear()

    again = await store.get("s1")
    assert again is not None
    assert len(again.messages) == 2


async def test_bad_row_does_not_drop_the_rest(tmp_path: Path):
    path = tmp_path / "s.db"
    store = SQLiteSessionStore(path)
    await store.create(_session("ok"))
    await store.aclose()

    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO sessions (id, tenant, data) VALUES (?, ?, ?)",
        ("bad", "default", "{ 这不是 JSON"),
    )
    conn.commit()
    conn.close()

    reopened = SQLiteSessionStore(path)
    assert [item.id for item in await reopened.list()] == ["ok"]
    assert await reopened.get("bad") is None
    await reopened.aclose()


async def test_concurrent_writes_all_land(tmp_path: Path):
    """连接是共享的：并发写入必须在锁上串行化，不能互相踩掉。"""
    store = SQLiteSessionStore(tmp_path / "s.db")
    await asyncio.gather(*(store.create(_session(f"s{i}")) for i in range(20)))

    assert len(await store.list()) == 20
    await store.aclose()


async def test_ping_and_repeated_close(tmp_path: Path):
    store = SQLiteSessionStore(tmp_path / "s.db")
    assert await store.ping() is True
    await store.aclose()
    await store.aclose()  # 退出路径可能关两次，不能抛
    assert await store.ping() is False


async def test_import_legacy_sessions(tmp_path: Path):
    """首次切到 SQLite 时要自动接管旧 JSON：升级后历史会话不能凭空消失。"""
    source = tmp_path / "sessions.json"
    payload = {
        "version": 1,
        "sessions": [_session("a1", "acme").to_dict(), _session("g1", "globex").to_dict()],
    }
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert import_legacy_sessions(source, tmp_path / "sessions.db") == 2

    store = SQLiteSessionStore(tmp_path / "sessions.db")
    assert sorted(item.id for item in await store.list("acme")) == ["a1"]
    assert [item.id for item in await store.list("globex")] == ["g1"]
    await store.aclose()

    # 幂等：重复导入同一个文件不会翻倍
    assert import_legacy_sessions(source, tmp_path / "sessions.db") == 2
    check = SQLiteSessionStore(tmp_path / "sessions.db")
    assert len(await check.list()) == 2
    await check.aclose()
