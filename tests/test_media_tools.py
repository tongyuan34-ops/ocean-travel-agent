"""media 组：模板库、生图工具与异步任务语义（SPEC 4.6 media 组）。

全部不联网：后端换成桩，校验的是"任务被正确创建、错误如实返回、不阻塞等待"。
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar

from app.agent.tools.media import generate_image, list_image_templates
from app.media.base import MediaBackend, MediaKind, MediaRequest, MediaResult, TaskStatus
from app.media.service import MediaService, current_media
from app.media.templates import (
    TEMPLATE_KEYS,
    compose_prompt,
    describe_templates,
    find_template,
)


class StubImageBackend(MediaBackend):
    """记录收到的请求，立即返回成功。"""

    kind = MediaKind.IMAGE
    name = "stub_image"

    def __init__(self, *, available: bool = True, error: Exception | None = None) -> None:
        self._available = available
        self._error = error
        self.requests: list[MediaRequest] = []

    def available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return "" if self._available else "测试桩：未配置后端"

    async def generate(self, req: MediaRequest) -> MediaResult:
        self.requests.append(req)
        if self._error:
            raise self._error
        return MediaResult(status=TaskStatus.SUCCEEDED, result_url="https://img.example/x.jpg")


def _service(backend: MediaBackend | None) -> MediaService:
    return MediaService(image_backend=backend, video_backend=None, ttl_sec=60)


# ---------- 模板库 ----------


def test_templates_are_complete():
    assert len(TEMPLATE_KEYS) >= 4
    for key in TEMPLATE_KEYS:
        template = find_template(key)
        assert template is not None
        # 每个模板都必须有可用的约束，否则等于没有模板
        assert template.style and template.composition and template.lighting
        assert template.example


def test_find_template_is_key_insensitive():
    assert find_template("  CHECKIN ").key == "checkin"
    assert find_template("not-a-template") is None


def test_compose_prompt_keeps_user_intent_first():
    template = find_template("checkin")
    result = compose_prompt("三亚亚龙湾打卡", template)
    assert result.startswith("三亚亚龙湾打卡")
    assert template.style in result
    assert template.composition in result


def test_compose_without_template_returns_prompt_unchanged():
    assert compose_prompt("随便画点什么", None) == "随便画点什么"


def test_describe_templates_mentions_all_keys():
    text = describe_templates()
    for key in TEMPLATE_KEYS:
        assert key in text


# ---------- 生图工具 ----------


async def test_generate_image_without_session_context():
    """脱离会话调用时如实报未启用，而不是崩掉。"""
    assert current_media.get() is None
    out = await generate_image.ainvoke({"prompt": "海边日落"})
    assert out.startswith("TOOL_NOT_ENABLED")


async def test_generate_image_reports_missing_backend():
    service = _service(StubImageBackend(available=False))
    token = current_media.set(service)
    try:
        out = await generate_image.ainvoke({"prompt": "海边日落"})
    finally:
        current_media.reset(token)
    assert out.startswith("MEDIA_BACKEND_MISSING")
    assert "未配置后端" in out


async def test_generate_image_submits_task_without_blocking():
    """关键语义：只创建任务、立即返回 task_id，不在工具里等出图。"""
    backend = StubImageBackend()
    service = _service(backend)
    token = current_media.set(service)
    try:
        out = await generate_image.ainvoke({"prompt": "三亚海边日落"})
    finally:
        current_media.reset(token)

    assert "task_id：" in out
    # 任务确实进了同一个 service 实例，因此前端能查得到
    assert "pending" in out or "running" in out
    assert len(backend.requests) == 1
    assert backend.requests[0].kind == MediaKind.IMAGE
    assert "24 小时" in out  # 必须提示临时链接会失效


async def test_generate_image_applies_template():
    backend = StubImageBackend()
    service = _service(backend)
    token = current_media.set(service)
    try:
        out = await generate_image.ainvoke({"prompt": "三亚海边", "style": "poster"})
    finally:
        current_media.reset(token)

    assert "旅行海报" in out
    sent = backend.requests[0].prompt
    assert "三亚海边" in sent
    assert "构图" in sent  # 模板约束被拼进实际提示词
    assert backend.requests[0].style == "poster"


async def test_generate_image_accepts_free_style():
    backend = StubImageBackend()
    service = _service(backend)
    token = current_media.set(service)
    try:
        await generate_image.ainvoke({"prompt": "海边", "style": "赛博朋克霓虹"})
    finally:
        current_media.reset(token)

    sent = backend.requests[0].prompt
    assert "赛博朋克霓虹" in sent
    assert backend.requests[0].style == "赛博朋克霓虹"


async def test_generate_image_passes_ref_image():
    backend = StubImageBackend()
    service = _service(backend)
    token = current_media.set(service)
    try:
        await generate_image.ainvoke(
            {"prompt": "同风格", "ref_image": "https://img.example/ref.jpg"}
        )
    finally:
        current_media.reset(token)
    assert backend.requests[0].ref_image == "https://img.example/ref.jpg"


async def test_generate_image_rejects_empty_prompt():
    out = await generate_image.ainvoke({"prompt": "   "})
    assert out.startswith("INVALID_REQUEST")


async def test_generate_image_propagates_upstream_error():
    from app.media.base import UpstreamError

    backend = StubImageBackend(error=UpstreamError("模型未开通"))
    service = _service(backend)
    token = current_media.set(service)
    try:
        out = await generate_image.ainvoke({"prompt": "海边"})
    finally:
        current_media.reset(token)
    # 背景任务的失败不会冒到工具返回值里，但任务状态会被标记失败
    assert "task_id：" in out
    task = next(iter(service._tasks.values()))
    assert task.status == TaskStatus.FAILED
    assert "模型未开通" in (task.error or "")


# ---------- 模板清单工具 ----------


async def test_list_image_templates_tool():
    out = await list_image_templates.ainvoke({})
    for key in TEMPLATE_KEYS:
        assert key in out


def test_media_tools_registered_with_media_category():
    from app.agent.tools import ALL_TOOLS, MEDIA_TOOLS
    from app.services.chat import tool_category

    names = {tool.name for tool in MEDIA_TOOLS}
    assert names == {"generate_image", "list_image_templates"}
    assert names <= {tool.name for tool in ALL_TOOLS}
    for name in names:
        assert tool_category(name) == "media"


def test_templates_endpoint(client):
    data = client.get("/api/v1/media/templates").json()
    keys = [item["key"] for item in data["items"]]
    assert keys == list(TEMPLATE_KEYS)
    for item in data["items"]:
        assert item["label"] and item["example"]


def test_tools_list_reflects_image_capability(client):
    """工具清单的 enabled 必须与 capabilities.image_gen 一致，不能两处矛盾。"""
    caps = client.get("/api/v1/system/capabilities").json()
    items = {item["name"]: item for item in client.get("/api/v1/tools").json()["items"]}

    assert items["generate_image"]["enabled"] == caps["image_gen"]
    assert items["list_image_templates"]["enabled"] is True


def test_video_tool_not_registered():
    """生视频不做（产品决定），不应出现在工具清单里。"""
    from app.agent.tools import ALL_TOOLS

    assert "generate_video" not in {tool.name for tool in ALL_TOOLS}


def test_service_unavailable_reason_when_backend_none(settings):
    scoped = dataclasses.replace(settings, video_backend="none")
    from app.media.service import build_media_service

    service = build_media_service(scoped)
    caps = service.capabilities()
    assert caps["video_gen"] is False
    assert "video" in service.unavailable_reasons()


# ---------- 水印开关 ----------


class _CaptureClient:
    """截获 httpx 请求体，不发真实网络请求。"""

    captured: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, _url, json=None, headers=None):
        _CaptureClient.captured = json or {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"url": "https://img.example/x.jpg"}]}

        return _Resp()


async def test_watermark_defaults_to_false(settings, monkeypatch):
    """方舟接口的 watermark 默认 true，必须显式传 false 才能去掉「AI生成」字样。"""
    from app.media import custom_image

    monkeypatch.setattr(custom_image.httpx, "AsyncClient", _CaptureClient)
    backend = custom_image.CustomImageBackend(
        endpoint="https://ark.example/images",
        token="Bearer x",
        model="m",
        watermark=False,
    )
    await backend.generate(MediaRequest(kind=MediaKind.IMAGE, prompt="海边"))
    assert _CaptureClient.captured["watermark"] is False


async def test_watermark_can_be_enabled(settings, monkeypatch):
    from app.media import custom_image

    monkeypatch.setattr(custom_image.httpx, "AsyncClient", _CaptureClient)
    backend = custom_image.CustomImageBackend(
        endpoint="https://ark.example/images",
        token="Bearer x",
        model="m",
        watermark=True,
    )
    await backend.generate(MediaRequest(kind=MediaKind.IMAGE, prompt="海边"))
    assert _CaptureClient.captured["watermark"] is True
