"""HTTP 契约层：health / capabilities / tools / 会话 / 权限裁决与错误码映射。

对应 SPEC 第二部分契约与 2.3 错误规范。
"""

from __future__ import annotations

import io
from urllib.parse import quote

from app.adapters.mediacrawler import SOCIAL_PLATFORM_KEYS
from app.agent.tools import ALL_TOOLS
from app.core import secrets
from app.core.config import KEY_NAMES

_VALID_CATEGORIES = {
    "info",
    "search",
    # 知识库检索单独一类：它查的是规则，search 组查的是实时事实
    "knowledge",
    "plan",
    "media",
    "system",
    # 文档能力单独一类：它和 system 都碰文件，但一个读写字节、一个理解结构，
    # 前端展示和提示词里都分开讲
    "docs",
    "plugin",
    # 外部 MCP 工具：能出现在清单里说明连接成功
    "mcp",
    # 元工具（discover_tools）：它不是业务能力，只是「申请更多工具」的入口，
    # 前端按 category 分组展示时单独放，不该混进任何业务组
    "meta",
    # 技能自带的工具：名字以 skill__ 开头，只在技能选中时绑定
    "skill",
    "other",
}


def test_health(client):
    data = client.get("/api/v1/health").json()
    assert data["status"] == "ok"
    assert {"version", "model", "provider"} <= set(data)
    # 技能数与知识库片段数一样，是打包冒烟依赖的运行时可验证信号
    assert isinstance(data.get("skills"), int)


def test_capabilities_reports_key_sources_without_values(client):
    """前端要知道 key 是哪来的，但**绝不能**知道 key 是什么。

    桌面版用户看到「额度用尽」时，得能判断是自己没填还是内置的用完了。
    """
    data = client.get("/api/v1/system/capabilities").json()

    sources = data["key_sources"]
    assert set(sources) == set(KEY_NAMES)
    assert all(source in {"env", "config", "bundled", ""} for source in sources.values())
    assert isinstance(data["using_bundled_keys"], list)


def test_using_bundled_keys_names_the_ones_in_use(client, monkeypatch):
    """用内置额度的项要单独列出来，否则用户不知道该去填哪个。"""
    monkeypatch.delenv("BING_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "bundled", lambda: {"BING_SEARCH_API_KEY": "placeholder"})

    data = client.get("/api/v1/system/capabilities").json()

    assert data["key_sources"]["BING_SEARCH_API_KEY"] == "bundled"
    assert data["using_bundled_keys"] == ["BING_SEARCH_API_KEY"]


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


def test_file_tools_stay_enabled_before_any_grant(client, monkeypatch):
    """一个授权目录都没有时，文件工具仍然要报**可用**。

    按需授权的意思是「第一次用的时候问」，不是「没授权就不能用」。这里如果
    按 ``allowed_roots`` 判空，刚装好的机器上文件功能整体变灰——用户连触发
    授权弹窗的入口都没有，功能看起来就是坏的。

    这里把 allowed_roots 打成空，模拟新装机器的状态。
    """
    import app.api.routes.system as system_route
    import app.core.security as security

    monkeypatch.setattr(security, "allowed_roots", lambda settings: [])
    monkeypatch.setattr(system_route, "allowed_roots", lambda settings: [])

    items = {item["name"]: item for item in client.get("/api/v1/tools").json()["items"]}
    assert items["write_file"]["enabled"] is True
    assert items["read_file"]["enabled"] is True

    data = client.get("/api/v1/system/capabilities").json()
    assert data["filesystem"] is True
    # 回执仍然是空的：可用 ≠ 已经放开
    assert data["allowed_roots"] == []


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
        # 文档组里会动用户资料的两个：改文档写副本、整理目录批量移动
        "edit_document",
        "organize_documents",
    }
    # 只读工具不在需确认清单里（契约 2.4）。文档的读和概览也是只读，同样不该问
    assert not permitted & {
        "list_dir",
        "read_file",
        "read_document",
        "digest_documents",
        "query_sheet",
        "aggregate_sheet",
    }


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


# ---------- 附件：文档（Word / Excel） ----------


def _docx_bytes(text: str = "云南五日游服务合同") -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(text, level=1)
    document.add_paragraph("甲方：{{客户名}}")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["客户", "城市", "金额"])
    for row in [("张三", "上海", 5200), ("李四", "三亚", 1800)]:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_upload_docx_kept_byte_for_byte(client, tmp_path):
    """文档不重新编码：Word 是 zip 包，任何转码都只会把它弄坏。"""
    client.app.state.uploads.root = tmp_path
    raw = _docx_bytes()

    resp = client.post(
        "/api/v1/media/uploads",
        content=raw,
        headers={
            # 浏览器给 Office 文件的类型极不可靠，这里故意给个错的
            "Content-Type": "application/octet-stream",
            "X-File-Name": quote("云南合同.docx"),
        },
    )

    assert resp.status_code == 200
    info = resp.json()
    assert info["kind"] == "doc"
    assert info["filename"] == "云南合同.docx"
    assert info["format"] == "docx"
    assert info["bytes"] == len(raw)
    assert info["width"] == 0 and info["height"] == 0

    stored = client.get(info["url"])
    assert stored.status_code == 200
    assert stored.content == raw  # 一个字节都没变
    assert "wordprocessingml" in stored.headers["content-type"]
    assert stored.headers["content-disposition"].startswith("attachment")


def test_upload_xlsx_kept_byte_for_byte(client, tmp_path):
    client.app.state.uploads.root = tmp_path
    raw = _xlsx_bytes()

    resp = client.post(
        "/api/v1/media/uploads",
        content=raw,
        headers={"Content-Type": "application/octet-stream", "X-File-Name": "orders.xlsx"},
    )

    assert resp.status_code == 200
    info = resp.json()
    assert info["kind"] == "doc"
    stored = client.get(info["url"])
    assert stored.content == raw
    assert "spreadsheetml" in stored.headers["content-type"]


def test_upload_docx_with_wrong_content_rejected(client, tmp_path):
    """扩展名写 .docx 但内容不是 zip 包——不能老老实实存下来等后面解析时炸。"""
    client.app.state.uploads.root = tmp_path

    resp = client.post(
        "/api/v1/media/uploads",
        content=b"MZ\x90\x00this is not a docx",
        headers={"Content-Type": "application/octet-stream", "X-File-Name": quote("假的.docx")},
    )

    assert resp.status_code == 415
    assert resp.json()["code"] == "UPLOAD_TYPE_NOT_ALLOWED"
    assert "内容与扩展名不符" in resp.json()["message"]


def test_upload_legacy_doc_is_kept(client, tmp_path):
    """2003 格式读不了，但要收下来——直接拒收只会让用户以为软件坏了。"""
    client.app.state.uploads.root = tmp_path

    resp = client.post(
        "/api/v1/media/uploads",
        content=b"\xd0\xcf\x11\xe0legacy doc",
        headers={"Content-Type": "application/msword", "X-File-Name": quote("旧合同.doc")},
    )

    assert resp.status_code == 200
    assert resp.json()["kind"] == "doc"
    assert resp.json()["format"] == "doc"


def test_upload_rejects_unknown_document_suffix(client, tmp_path):
    client.app.state.uploads.root = tmp_path

    resp = client.post(
        "/api/v1/media/uploads",
        content=b"%PDF-1.7",
        headers={"Content-Type": "application/pdf", "X-File-Name": quote("报价单.pdf")},
    )

    # PDF 不在白名单里，按图片处理会 415；关键是不能悄悄存下来
    assert resp.status_code == 415


def test_upload_doc_size_limit_is_larger_than_image(client, tmp_path):
    """文档上限 32MB，比图片的 12MB 宽——带图的 docx 很容易过 12MB。"""
    from app.media.uploads import MAX_DOC_BYTES, MAX_UPLOAD_BYTES

    assert MAX_DOC_BYTES > MAX_UPLOAD_BYTES


def test_history_carries_attachment_meta(client, tmp_path, monkeypatch):
    """刷新后前端只有一串 id，靠 attachment_meta 才知道该画缩略图还是文件卡片。"""
    from app.storage import ChatMessage, ChatSession

    client.app.state.uploads.root = tmp_path
    upload = client.post(
        "/api/v1/media/uploads",
        content=_xlsx_bytes(),
        headers={"Content-Type": "application/octet-stream", "X-File-Name": "orders.xlsx"},
    ).json()

    session = ChatSession(
        id="s-meta",
        created_at="2026-11-11T12:00:00Z",
        messages=[
            ChatMessage(
                id="m-1",
                role="user",
                content="看看这份订单",
                created_at="2026-11-11T12:00:00Z",
                attachments=[upload["upload_id"]],
            )
        ],
    )
    sessions = client.app.state.sessions

    async def fake_get_session(_session_id: str, _tenant: str | None = None) -> ChatSession:
        return session

    # 直接替换取会话这一步：这个用例要验的是「读历史时会不会补附件元数据」，
    # 不是存储层。走真实 store 还得先造一条历史，反而把用例变成了存储测试
    monkeypatch.setattr(sessions, "get", fake_get_session)

    item = client.get("/api/v1/chat/sessions/s-meta/messages").json()["items"][0]
    assert item["attachments"] == [upload["upload_id"]]
    assert item["attachment_meta"][upload["upload_id"]]["kind"] == "doc"
    assert item["attachment_meta"][upload["upload_id"]]["filename"] == "orders.xlsx"
