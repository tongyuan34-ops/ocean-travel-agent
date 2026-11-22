"""单端口入口：静态页 + API 合在一个进程，外面套一层 Basic 鉴权。

这里最要紧的一条不是鉴权，是**不能缓冲响应体**。
最初用 ``@app.middleware("http")`` 写的，实测 38 个 SSE 事件全部在 8.6 秒
一次性到达——逐字输出变成「转圈 8 秒然后整段蹦出来」。所以下面有一条用例
专门盯着「第一块响应体是不是立刻转发」，而不是只看最终状态码。
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.standalone import BasicAuthMiddleware

USER = "ocean"
PASSWORD = "s3cret"


def _app(**kwargs: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/chat/sessions")
    async def sessions() -> dict[str, list[str]]:
        return {"items": []}

    @app.get("/serve.py")
    async def source() -> dict[str, str]:
        return {"should": "never be reachable"}

    app.add_middleware(BasicAuthMiddleware, user=USER, password=PASSWORD, **kwargs)
    return app


def _auth(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_no_credentials_gets_401():
    response = TestClient(_app()).get("/api/v1/chat/sessions")

    assert response.status_code == 401
    # 少了这个头，浏览器不会弹登录框，用户只会看到一个 401 空白页
    assert response.headers["www-authenticate"].startswith("Basic")


def test_wrong_password_gets_401():
    response = TestClient(_app()).get("/api/v1/chat/sessions", headers=_auth(password="nope"))

    assert response.status_code == 401


def test_wrong_user_gets_401():
    response = TestClient(_app()).get("/api/v1/chat/sessions", headers=_auth(user="someone"))

    assert response.status_code == 401


def test_correct_credentials_pass():
    response = TestClient(_app()).get("/api/v1/chat/sessions", headers=_auth())

    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    ["Bearer abc", "Basic !!!not-base64!!!", "Basic", ""],
)
def test_malformed_headers_are_rejected_not_crashed(header: str):
    """畸形头必须走 401，不能抛异常变成 500。

    攻击面就在这里：随便一个扫描器都会拿垃圾 Authorization 头打一遍，
    500 会把栈和路径回显出去。
    """
    response = TestClient(_app()).get("/api/v1/chat/sessions", headers={"Authorization": header})

    assert response.status_code == 401


def test_health_is_public():
    """探活不鉴权：托管平台的健康检查没法带账号密码。"""
    response = TestClient(_app()).get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/serve.py", "/web/serve.py"])
def test_source_files_are_not_served(path: str):
    """静态目录里混着 serve.py 这类源码，不该能被下载走。

    这里返回 404 而不是 401：鉴权通过的人也不该看到它，
    否则等于把部署脚本、内网地址一起公开。
    """
    response = TestClient(_app()).get(path, headers=_auth())

    assert response.status_code == 404


async def test_body_chunks_are_forwarded_immediately():
    """第一块响应体必须**在应用还卡着的时候**就转发出去。

    缓冲型中间件（``BaseHTTPMiddleware``）会等应用返回后才一次性 send，
    那时下面断言看到的还是空列表——这正是 SSE 变成「一次性吐出」的原因。
    """
    seen: list[bytes] = []
    gate = asyncio.Event()

    async def streaming_app(scope: dict, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        await gate.wait()  # 模拟「模型还在生成下一个 token」
        await send({"type": "http.response.body", "body": b"data: two\n\n", "more_body": False})

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            seen.append(message["body"])

    middleware = BasicAuthMiddleware(streaming_app, USER, PASSWORD)
    scope = {
        "type": "http",
        "path": "/api/v1/chat/sessions/x/messages",
        "headers": [(b"authorization", _auth()["Authorization"].encode())],
    }

    task = asyncio.create_task(middleware(scope, lambda: None, send))
    for _ in range(10):
        await asyncio.sleep(0)

    assert seen == [b"data: one\n\n"], "第一块被扣住了：中间件在缓冲响应体"
    assert not task.done()

    gate.set()
    await task
    assert seen == [b"data: one\n\n", b"data: two\n\n"]


async def test_non_http_scopes_pass_through():
    """lifespan / websocket 必须原样透传。

    拦了 lifespan 的话应用启动事件不执行，``app.state`` 上没有 media、
    chat、session_store，接口全部 AttributeError——而且日志里只有一句
    「启动完成」，看不出是中间件吃掉的。
    """
    handled: list[str] = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        handled.append(scope["type"])

    middleware = BasicAuthMiddleware(app, USER, PASSWORD)

    await middleware({"type": "lifespan"}, lambda: None, lambda _: None)
    await middleware({"type": "websocket", "path": "/ws"}, lambda: None, lambda _: None)

    assert handled == ["lifespan", "websocket"]


def test_frontend_and_api_share_one_port(monkeypatch):
    """同一个进程既给页面也给接口——托管平台只开一个端口。

    这条用例真正盯的是 ``WEB_DIR`` 解析对不对：打包成单文件后静态资源在
    ``_MEIPASS`` 里，而可写目录是 exe 旁边，两者混用会出现「接口正常、
    页面 404」或者「exe 旁边找不到 web/」。
    """
    monkeypatch.setenv("WEB_AUTH_PASS", "pw")
    # 走内存后端：这条用例只关心挂载，不该在仓库里留下 data/sessions.json
    monkeypatch.setenv("STORAGE_BACKEND", "memory")

    from app.standalone import build_standalone_app

    # 必须进 lifespan：``app.state.settings`` 是在启动事件里挂上去的，
    # 不进入上下文，/health 会直接 AttributeError
    with TestClient(build_standalone_app()) as client:
        assert client.get("/").status_code == 401
        page = client.get("/", auth=("ocean", "pw"))
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        # 前端走同源相对路径，所以接口必须在同一端口下可达
        assert client.get("/api/v1/health", auth=("ocean", "pw")).status_code == 200


def test_missing_password_refuses_to_start(monkeypatch):
    """没密码就不许起。

    原来这里只 ``logger.warning`` 一句就继续服务，而这个入口按文档就是
    ``--host 0.0.0.0``——警告在控制台滚过去，服务照常对外，等于没有防线。
    失败方向必须收敛到「不提供服务」：起不来是当场能发现的事故。
    """
    monkeypatch.delenv("WEB_AUTH_PASS", raising=False)
    monkeypatch.delenv("ALLOW_OPEN_ACCESS", raising=False)

    from app.standalone import build_standalone_app

    with pytest.raises(RuntimeError) as caught:
        build_standalone_app()

    message = str(caught.value)
    assert "WEB_AUTH_PASS" in message
    # 报错要给出下一步动作，否则用户只知道「起不来」
    assert "ALLOW_OPEN_ACCESS" in message


def test_open_access_requires_an_explicit_opt_in(monkeypatch):
    """确实要裸跑的场景（内网、前面有反代鉴权）留一个显式开关。"""
    monkeypatch.delenv("WEB_AUTH_PASS", raising=False)
    monkeypatch.setenv("ALLOW_OPEN_ACCESS", "1")
    monkeypatch.setenv("STORAGE_BACKEND", "memory")

    from app.standalone import build_standalone_app

    with TestClient(build_standalone_app()) as client:
        assert client.get("/api/v1/health").status_code == 200


def test_desktop_mode_still_needs_no_password(monkeypatch):
    """桌面版只监听 127.0.0.1，不该被这条规则挡住。"""
    monkeypatch.delenv("WEB_AUTH_PASS", raising=False)
    monkeypatch.delenv("ALLOW_OPEN_ACCESS", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "memory")

    from app.standalone import build_standalone_app

    with TestClient(build_standalone_app(require_auth=False)) as client:
        assert client.get("/api/v1/health").status_code == 200


def test_share_link_does_not_require_basic_auth(monkeypatch):
    """分享页的凭据是 URL 里的 token。

    要求 Basic Auth 会让「把行程发给朋友」直接失效，而失效时页面只弹一个
    登录框，用户根本想不到是密码的问题。
    """
    monkeypatch.setenv("WEB_AUTH_PASS", "pw")
    monkeypatch.setenv("STORAGE_BACKEND", "memory")

    from app.standalone import build_standalone_app

    with TestClient(build_standalone_app()) as client:
        response = client.get("/share/not-a-real-token")
        # token 不存在 → 404 或 410 都行，但绝不能是 401
        assert response.status_code != 401
