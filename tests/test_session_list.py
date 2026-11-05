"""会话列表：后端出列表，前端不再自己记账。

**为什么单独一组用例**

会话数据一直存在盘上，坏掉的只是「怎么找到它」：桌面版每次启动端口随机，
而 localStorage 按源（含端口）隔离，重开程序等于换了个新源——前端记的列表
和「上次聊到哪个会话」都是空的。用户看到的是「历史消息全没了」。

所以这里的断言都围绕一件事：**列表和标题必须能从后端拿到**，跟浏览器
缓存、跟端口、跟进程重启都没关系。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.storage.file import FileSessionStore
from app.storage.models import ChatMessage, ChatSession


def _msg(role: str, content: str, created_at: str) -> ChatMessage:
    return ChatMessage(id=f"m-{created_at}", role=role, content=content, created_at=created_at)


def _session(session_id: str, first_user: str, created_at: str) -> ChatSession:
    return ChatSession(
        id=session_id,
        created_at=created_at,
        messages=[
            _msg("user", first_user, created_at),
            _msg("assistant", "好的，我来排。", created_at),
        ],
    )


def test_file_store_lists_sessions_after_restart(tmp_path: Path) -> None:
    """落盘的会话，重启后要能被列出来——列表和消息走的是同一份数据。"""
    path = tmp_path / "sessions.json"
    first = FileSessionStore(path)
    asyncio.run(first.create(_session("s1", "湄洲岛两天", "2026-11-05T09:00:00+00:00")))
    asyncio.run(first.create(_session("s2", "三亚亲子四天", "2026-11-05T10:00:00+00:00")))

    second = FileSessionStore(path)
    listed = asyncio.run(second.list())

    assert sorted(session.id for session in listed) == ["s1", "s2"]
    # 消息也要跟着回来，只有 id 的列表点进去还是空的
    assert all(session.messages for session in listed)


def test_sessions_endpoint_is_newest_first_with_derived_title(client) -> None:
    """标题从首条用户消息里取，顺序按最后一条消息倒序。"""
    older = client.post("/api/v1/chat/sessions").json()
    newer = client.post("/api/v1/chat/sessions").json()

    for session, text in ((older, "预算怎么分"), (newer, "带两岁小孩怎么安排节奏")):
        resp = client.post(
            f"/api/v1/chat/sessions/{session['session_id']}/messages",
            json={"message": text},
        )
        assert resp.status_code in (200, 201, 202), resp.text

    items = client.get("/api/v1/chat/sessions").json()["items"]
    ids = [item["id"] for item in items]
    assert ids.index(newer["session_id"]) < ids.index(older["session_id"])

    by_id = {item["id"]: item for item in items}
    assert by_id[older["session_id"]]["title"] == "预算怎么分"
    assert by_id[newer["session_id"]]["title"].startswith("带两岁小孩")
    assert by_id[older["session_id"]]["message_count"] >= 1


def test_session_without_messages_still_has_a_title(client) -> None:
    """空会话也要能出现在列表里，标题给默认值而不是空字符串。"""
    created = client.post("/api/v1/chat/sessions").json()

    items = client.get("/api/v1/chat/sessions").json()["items"]
    by_id = {item["id"]: item for item in items}

    assert by_id[created["session_id"]]["title"] == "新的旅程"
    assert by_id[created["session_id"]]["message_count"] == 0


def test_long_first_message_is_truncated() -> None:
    """侧栏一行放不下长标题；截断规则要和前端发送后显示的那版一致。"""
    from app.services.chat import _session_title

    session = _session("s1", "请" * 60, "2026-11-05T09:00:00+00:00")

    title = _session_title(session)
    assert len(title) == 21  # 20 字 + 省略号
    assert title.endswith("…")
