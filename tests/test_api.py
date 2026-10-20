"""HTTP 契约层：health / capabilities / tools / 会话 / 权限裁决与错误码映射。

对应 SPEC 第二部分契约与 2.3 错误规范。
"""

from __future__ import annotations

from app.adapters.mediacrawler import SOCIAL_PLATFORM_KEYS
from app.agent.tools import ALL_TOOLS

_VALID_CATEGORIES = {"info", "search", "plan", "media", "system", "plugin", "other"}


def test_health(client):
    data = client.get("/api/v1/health").json()
    assert data["status"] == "ok"
    assert {"version", "model", "provider"} <= set(data)


def test_capabilities_shape_and_consistency(client):
    data = client.get("/api/v1/system/capabilities").json()

    for key in (
        "filesystem",
        "wallpaper",
        "image_gen",
        "video_gen",
        "link_parse",
        "search_web",
        "search_social",
        "session_fetch",
    ):
        assert isinstance(data[key], bool), key

    assert isinstance(data["platforms"], list)
    assert {"bing_cn", "web_fetch"} <= set(data["platforms"])

    # search_social 必须与 platforms 里的社媒平台一致，不能自相矛盾
    social_in_platforms = set(data["platforms"]) & set(SOCIAL_PLATFORM_KEYS)
    assert data["search_social"] == bool(social_in_platforms)


def test_tools_list_matches_registry(client):
    items = client.get("/api/v1/tools").json()["items"]

    assert {item["name"] for item in items} == {tool.name for tool in ALL_TOOLS}
    for item in items:
        assert item["category"] in _VALID_CATEGORIES, item
        assert isinstance(item["enabled"], bool)
        assert item["description"].strip()

    permitted = {item["name"] for item in items if item["requires_permission"]}
    assert permitted == {"write_file", "create_dir", "delete_path", "set_wallpaper"}
    # 只读工具不在需确认清单里（契约 2.4）
    assert not permitted & {"list_dir", "read_file"}


def test_session_lifecycle(client):
    created = client.post("/api/v1/chat/sessions").json()
    session_id = created["session_id"]
    assert created["created_at"]

    assert client.get(f"/api/v1/chat/sessions/{session_id}/messages").json()["items"] == []

    assert client.delete(f"/api/v1/chat/sessions/{session_id}").status_code == 204

    gone = client.get(f"/api/v1/chat/sessions/{session_id}/messages")
    assert gone.status_code == 404
    assert gone.json()["code"] == "SESSION_NOT_FOUND"


def test_send_message_validates_input(client):
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]

    empty = client.post(f"/api/v1/chat/sessions/{session_id}/messages", json={"message": ""})
    assert empty.status_code == 422
    assert empty.json()["code"] == "VALIDATION_ERROR"

    too_long = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages", json={"message": "x" * 4001}
    )
    assert too_long.status_code == 422


def test_send_message_unknown_session(client):
    resp = client.post("/api/v1/chat/sessions/not-exist/messages", json={"message": "hi"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


def test_permission_decision_unknown_request(client):
    resp = client.post(
        "/api/v1/permissions/not-a-real-id/decision", json={"granted": True}
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"
    assert resp.json()["message"]
