"""多租户隔离：会话在创建时盖章，之后只对同一个租户可见。

**为什么单独一组用例**

隔离漏一处就是一个越权入口，而且漏了不会报错——只是"别人的会话出现在我的
列表里"。所以这里逐条覆盖每个会碰到会话的接口：列表、消息、用量、风格、
删除、发消息。越权一律按 404 处理（不是 403）：403 等于告诉对方
"这个 id 是存在的"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.storage.file import FileSessionStore
from app.storage.models import ChatSession

_ACME = {"X-Tenant-ID": "acme"}
_GLOBEX = {"X-Tenant-ID": "globex"}
_T = "2026-11-16T09:00:00+00:00"


def _ids(client, headers: dict[str, str] | None = None) -> list[str]:
    resp = client.get("/api/v1/chat/sessions", headers=headers or {})
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


def test_sessions_are_isolated_by_tenant(client) -> None:
    """一个租户的会话不能出现在另一个租户的列表里。"""
    acme = client.post("/api/v1/chat/sessions", headers=_ACME).json()["session_id"]
    globex = client.post("/api/v1/chat/sessions", headers=_GLOBEX).json()["session_id"]

    assert acme in _ids(client, _ACME)
    assert globex not in _ids(client, _ACME)

    assert globex in _ids(client, _GLOBEX)
    assert acme not in _ids(client, _GLOBEX)

    # 裸请求（不带租户头）落到 default，两边都看不见
    plain = _ids(client)
    assert acme not in plain
    assert globex not in plain


def test_cross_tenant_access_is_not_found(client) -> None:
    """越权访问按"不存在"处理，且原租户的数据不受影响。"""
    session_id = client.post("/api/v1/chat/sessions", headers=_ACME).json()["session_id"]

    base = f"/api/v1/chat/sessions/{session_id}"
    cases = [
        client.get(f"{base}/messages", headers=_GLOBEX),
        client.get(f"{base}/usage", headers=_GLOBEX),
        client.patch(f"{base}/persona", json={"persona": "confidant"}, headers=_GLOBEX),
        client.delete(base, headers=_GLOBEX),
        client.post(f"{base}/messages", json={"message": "你好"}, headers=_GLOBEX),
    ]
    for resp in cases:
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "SESSION_NOT_FOUND"

    # 被拒绝的那几下不能真的删掉或改掉它
    assert client.get(f"{base}/messages", headers=_ACME).status_code == 200
    assert client.get(f"{base}/usage", headers=_ACME).json()["total_tokens"] == 0


def test_missing_or_invalid_tenant_falls_back_to_default(client) -> None:
    """老客户端不带租户头，仍然按默认租户正常工作。"""
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]

    assert session_id in _ids(client)
    assert session_id in _ids(client, {"X-Tenant-ID": "default"})
    # 非法值（含空格）同样回落，而不是 400
    assert session_id in _ids(client, {"X-Tenant-ID": "not a tenant"})


def test_file_store_persists_tenant_and_filters(tmp_path: Path) -> None:
    """落盘要带上租户；升级前没有 tenant 字段的老数据回落 default。"""
    path = tmp_path / "sessions.json"
    store = FileSessionStore(path)
    asyncio.run(store.create(ChatSession(id="s1", created_at=_T, tenant="acme")))
    asyncio.run(store.create(ChatSession(id="s2", created_at=_T, tenant="globex")))

    reopened = FileSessionStore(path)
    assert [item.id for item in asyncio.run(reopened.list("acme"))] == ["s1"]
    assert sorted(item.id for item in asyncio.run(reopened.list())) == ["s1", "s2"]

    legacy = ChatSession.from_dict({"id": "old", "created_at": "2026-11-01T00:00:00+00:00"})
    assert legacy.tenant == "default"
