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
    assert permitted == {
        "open_file",
        "write_file",
        "create_dir",
        "delete_path",
        "set_wallpaper",
    }
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

    # 支持「只发图不写字」之后，message 不再是必填，空值从 schema 校验下沉到路由判断：
    # 空字符串本身合法（配一张图就成立），「两者都没有」才是错，所以是 400 而不是 422
    empty = client.post(f"/api/v1/chat/sessions/{session_id}/messages", json={"message": ""})
    assert empty.status_code == 400
    assert empty.json()["code"] == "INVALID_REQUEST"

    too_long = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages", json={"message": "x" * 4001}
    )
    assert too_long.status_code == 422


def test_send_message_unknown_session(client):
    resp = client.post("/api/v1/chat/sessions/not-exist/messages", json={"message": "hi"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


def test_permission_decision_unknown_request(client):
    resp = client.post("/api/v1/permissions/not-a-real-id/decision", json={"granted": True})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"
    assert resp.json()["message"]


# ---------- 对话风格 ----------


def test_personas_endpoint_lists_four_styles(client):
    data = client.get("/api/v1/chat/personas").json()
    assert data["default"] == "concierge"
    keys = [item["key"] for item in data["items"]]
    assert keys == ["concierge", "companion", "guide", "confidant"]
    # 只给展示字段，不暴露 prompt 正文
    assert set(data["items"][0]) == {"key", "name", "latin", "tagline"}


def test_persona_defaults_on_new_session(client):
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]
    data = client.get(f"/api/v1/chat/sessions/{session_id}/messages").json()
    assert data["persona"] == "concierge"


def test_persona_switch_persists(client):
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]
    resp = client.patch(
        f"/api/v1/chat/sessions/{session_id}/persona", json={"persona": "confidant"}
    )
    assert resp.status_code == 200
    assert resp.json()["persona"] == "confidant"

    data = client.get(f"/api/v1/chat/sessions/{session_id}/messages").json()
    assert data["persona"] == "confidant"


def test_persona_unknown_falls_back_instead_of_failing(client):
    """未知 key 回落默认而不是 4xx：前端选项来自 /personas，
    真传错了让整轮对话失败也没好处。"""
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]
    resp = client.patch(f"/api/v1/chat/sessions/{session_id}/persona", json={"persona": "nope"})
    assert resp.status_code == 200
    assert resp.json()["persona"] == "concierge"


def test_persona_switch_on_missing_session_is_404(client):
    resp = client.patch(
        "/api/v1/chat/sessions/does-not-exist/persona", json={"persona": "confidant"}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


# ---------- 图片输入（多模态） ----------


def _png(width: int = 240, height: int = 180, alpha: bool = False) -> bytes:
    import io

    from PIL import Image

    mode = "RGBA" if alpha else "RGB"
    image = Image.new(mode, (width, height), (200, 40, 40, 255) if alpha else (200, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_capabilities_reports_vision(client):
    """前端靠这个字段决定要不要放「从相册选图」的入口。"""
    assert client.get("/api/v1/system/capabilities").json()["vision"] is True


def test_upload_roundtrip(client, tmp_path):
    """上传 → 取回 → 删除，三条路径都要通。"""
    client.app.state.uploads.root = tmp_path

    resp = client.post(
        "/api/v1/media/uploads",
        content=_png(3000, 2000),
        headers={"Content-Type": "image/png", "X-File-Name": "%E6%B5%B7%E8%BE%B9.png"},
    )
    assert resp.status_code == 200
    info = resp.json()
    assert info["width"] == 1280  # 长边被规范化，避免一张图吃掉整个上下文预算
    assert info["url"] == f"/api/v1/media/uploads/{info['upload_id']}"

    image = client.get(info["url"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")
    assert len(image.content) == info["bytes"]

    assert client.delete(info["url"]).status_code == 204
    assert client.get(info["url"]).status_code == 404


def test_upload_rejects_non_image(client, tmp_path):
    client.app.state.uploads.root = tmp_path
    resp = client.post(
        "/api/v1/media/uploads",
        content=b"<html>not an image</html>",
        headers={"Content-Type": "text/html"},
    )
    assert resp.status_code == 415
    assert resp.json()["code"] == "UPLOAD_TYPE_NOT_ALLOWED"


def test_upload_rejects_empty_body(client, tmp_path):
    client.app.state.uploads.root = tmp_path
    resp = client.post("/api/v1/media/uploads", content=b"", headers={"Content-Type": "image/png"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


def test_upload_accepts_octet_stream_when_name_says_image(client, tmp_path):
    """安卓相册经常给 application/octet-stream，这时候只能信扩展名。"""
    client.app.state.uploads.root = tmp_path
    resp = client.post(
        "/api/v1/media/uploads",
        content=_png(),
        headers={"Content-Type": "application/octet-stream", "X-File-Name": "a.jpg"},
    )
    assert resp.status_code == 200


def test_unknown_upload_id_is_404(client):
    assert client.get("/api/v1/media/uploads/" + "0" * 32).status_code == 404


def test_message_requires_text_or_attachment(client):
    """空消息进去只会让模型对着空气回答，还白占一条历史。"""
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]
    resp = client.post(f"/api/v1/chat/sessions/{session_id}/messages", json={"message": "  "})
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


def test_message_rejects_too_many_attachments(client):
    """上限写进 schema：前端拦一道，服务端也要拦一道。"""
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]
    resp = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"message": "看图", "attachments": ["0" * 32] * 7},
    )
    assert resp.status_code == 422
