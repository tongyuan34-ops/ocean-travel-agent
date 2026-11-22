"""令牌认证与跨租户边界：隔离之外还要「认得出你是谁」。

租户隔离只保证「不同租户的数据不互相串」，前提是租户身份可信。之前身份来自
``X-Tenant-ID``，任何人都能写 acme——隔离形同虚设。这里覆盖三件事：

- 令牌模式下身份只由 ``Authorization: Bearer`` 决定，请求头说了不算；
- 令牌缺失/错误一律 401，不静默降级成 default（default 里也存着数据）；
- 配额按租户单独计算，以及附件必须校验归属。
"""

from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.core.config import load_settings
from app.core.errors import ApiError
from app.core.identity import TenantTokenMiddleware, resolve_tenant_id, resolve_user_id
from app.media.uploads import UploadStore
from app.services.chat import ChatService
from app.services.sessions import SessionService
from app.storage.memory import MemorySessionStore
from test_attachments import _docx_bytes
from test_chat_sse import FakeGraph, _Chunk, _service

_TOKENS = {"acme": "tok_acme", "globex": "tok_globex"}


def _app(tokens: dict[str, str]) -> TestClient:
    """只装身份中间件的最小应用：把「解析出来的身份」原样回显。"""

    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse(
            {"tenant": resolve_tenant_id(request), "user": resolve_user_id(request)}
        )

    app = Starlette(
        routes=[
            Route("/whoami", whoami),
            Route("/api/v1/health", lambda _r: JSONResponse({"ok": True})),
            Route(
                "/api/v1/media/uploads/{upload_id}",
                lambda _r: JSONResponse({"ok": True}),
                methods=["GET", "DELETE"],
            ),
        ]
    )
    app.add_middleware(TenantTokenMiddleware, tokens=tokens)
    return TestClient(app)


def test_token_decides_tenant_and_ignores_header() -> None:
    client = _app(_TOKENS)

    resp = client.get(
        "/whoami",
        headers={"Authorization": "Bearer tok_acme", "X-Tenant-ID": "globex"},
    )

    assert resp.status_code == 200
    # 请求头说 globex，令牌说 acme：以令牌为准，否则隔离就是自欺欺人
    assert resp.json() == {"tenant": "acme", "user": "acme"}


def test_missing_or_wrong_token_is_401() -> None:
    client = _app(_TOKENS)

    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}):
        resp = client.get("/whoami", headers=headers)
        assert resp.status_code == 401, headers

    # 健康检查是容器探针用的，不能要令牌
    assert client.get("/api/v1/health").status_code == 200


def test_raw_upload_is_public_for_get_only() -> None:
    """``<img src>`` 带不了头，原图 GET 放开；同路径 DELETE 仍要令牌。"""
    client = _app(_TOKENS)

    assert client.get("/api/v1/media/uploads/abc").status_code == 200
    assert client.delete("/api/v1/media/uploads/abc").status_code == 401


def test_header_mode_still_works_without_tokens() -> None:
    """没配 TENANT_TOKENS 时保持旧行为，桌面版零配置可用。"""
    client = _app({})

    resp = client.get("/whoami", headers={"X-Tenant-ID": "acme", "X-Device-ID": "device-abc12345"})

    assert resp.json() == {"tenant": "acme", "user": "device-abc12345"}


async def test_per_tenant_concurrency_is_capped(monkeypatch):
    """一个租户吃满自己的额度后，别的租户照常能发；释放后恢复。"""
    monkeypatch.setenv("MAX_CONCURRENT_REPLIES", "10")
    monkeypatch.setenv("MAX_CONCURRENT_REPLIES_PER_TENANT", "1")
    service = _service(monkeypatch, _graph())

    acme_a = await service._sessions.create("acme")
    acme_b = await service._sessions.create("acme")
    globex = await service._sessions.create("globex")

    first = await service.start_reply(acme_a.id, "一", tenant="acme")

    with pytest.raises(ApiError) as caught:
        await service.start_reply(acme_b.id, "二", tenant="acme")

    assert caught.value.code == "RATE_LIMITED"
    assert "该租户" in caught.value.message

    # 另一个租户不该被牵连
    other = await service.start_reply(globex.id, "三", tenant="globex")

    for stream in (first, other):
        async for _ in service.subscribe(stream.stream_id, 0):
            pass

    # 释放后额度要还回来，否则那一轮结束后这个租户就再也发不出消息
    again = await service.start_reply(acme_b.id, "四", tenant="acme")
    async for _ in service.subscribe(again.stream_id, 0):
        pass


def _graph() -> FakeGraph:
    return FakeGraph(
        [
            {"event": "on_chat_model_stream", "data": {"chunk": _Chunk("好")}},
            {
                "event": "on_chain_end",
                "data": {"output": {"messages": [AIMessage(content="好")]}},
            },
        ]
    )


def test_attachment_must_belong_to_the_requester(tmp_path) -> None:
    """附件 id 是可传递的凭证：别人拿到 id 也不能把文件塞进自己的上下文。"""
    service = ChatService(
        load_settings(), SessionService(MemorySessionStore()), uploads=UploadStore(tmp_path)
    )
    mine = service._uploads.save_document(_docx_bytes(), "alice", "合同.docx")

    assert service._uploads.get(mine.id, "alice") is not None
    assert service._uploads.get(mine.id, "bob") is None

    blocks, images = service._context.attachment_blocks([mine.id], "bob")
    assert blocks == [] and images == []
    assert service.attachment_meta([mine.id], "bob") == {}

    # 归属校验不能把正常路径也挡掉
    blocks, _ = service._context.attachment_blocks([mine.id], "alice")
    assert blocks and "云南五日游服务合同" in blocks[0]["text"]
