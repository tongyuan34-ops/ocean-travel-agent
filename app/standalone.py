"""单端口入口：一个进程同时提供前端页面和 API。

**为什么需要它**

本地开发是两个进程：``uvicorn app.main:app``（8000）+ ``web/serve.py``（3001，
静态页 + ``/api/**`` 反代）。这么分有好处——前端不用构建、改完刷新即可，
反代也顺手做了同源和 Basic Auth。

但托管平台只给**一个 HTTP 端口**。所以这个模块把两件事合成一件：
同一个 FastAPI 既挂 ``/api/v1``，也把 ``web/`` 挂在根路径。前端本来就是
同源相对路径（``web/app.js`` 的 ``autoBase()`` 返回 ``/api/v1``），
所以页面不用改一行。

**鉴权也一起搬过来**

``serve.py`` 的 Basic Auth 只管静态页和反代，直连 8000 就绕过去了。
放到应用层之后，API 本身受保护——这是上线必须的：每次对话都在烧模型额度，
一条公开链接等于把 Key 交给所有人。

**为什么是纯 ASGI 中间件，而不是 ``@app.middleware("http")``**

后者包在 ``BaseHTTPMiddleware`` 里，会把响应体收齐了再往下发。
实测（本地 8002 端口，一句话提问）：38 个 SSE 事件全部在 8.6 秒一次性到达，
前端看到的是「转圈 8 秒，然后整段文字瞬间出现」——逐字输出完全没了。
纯 ASGI 中间件只在前面拦一道，放行后直接转发 ``send``，不碰响应体。

用户名密码沿用 ``WEB_AUTH_USER`` / ``WEB_AUTH_PASS``（与 ``serve.py`` 同名，
一份 .env 两边通用）。没配密码时不启用鉴权，只打一条告警——
本地开发不该被登录框挡住。

启动：
    PORT=8000 python -m uvicorn app.standalone:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from app.core.paths import RESOURCE_ROOT
from app.main import create_app

logger = logging.getLogger(__name__)

# 静态文件随程序分发，读 RESOURCE_ROOT（冻结后是 _MEIPASS）；
# 不能读 PROJECT_ROOT，那是 exe 旁边的可写目录，里面没有 web/
WEB_DIR = RESOURCE_ROOT / "web"

# 探活接口不鉴权：托管平台的健康检查、以及发布后的可达性验证都要打它，
# 让它们带上账号密码不现实。它只返回版本号和模型名，没有敏感信息。
_PUBLIC_PATHS = {"/api/v1/health"}

# 静态目录里混着 serve.py 这类源码文件，不该被当成静态资源下载走
_BLOCKED_SUFFIXES = (".py", ".pyc")

_DEFAULT_USER = "ocean"


def _credentials() -> tuple[str, str]:
    user = os.getenv("WEB_AUTH_USER", "").strip() or _DEFAULT_USER
    password = os.getenv("WEB_AUTH_PASS", "").strip()
    return user, password


def _authorized(header: str, user: str, password: str) -> bool:
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    got_user, _, got_password = decoded.partition(":")
    # 两个都比较，避免按用户名的耗时差反推出用户名
    user_ok = hmac.compare_digest(got_user, user)
    password_ok = hmac.compare_digest(got_password, password)
    return user_ok and password_ok


class BasicAuthMiddleware:
    """纯 ASGI 鉴权：放行时原样转发，不缓冲响应体。

    只处理 ``http`` scope；``lifespan`` 和 ``websocket`` 直接透传，
    否则应用启动事件会被这里吃掉，服务起不来还看不到原因。
    """

    def __init__(self, app: Any, user: str, password: str) -> None:
        self.app = app
        self.user = user
        self.password = password

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        if path.endswith(_BLOCKED_SUFFIXES):
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            await response(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization", "")
        if not _authorized(header, self.user, self.password):
            response = JSONResponse(
                {"detail": "需要访问密码"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ocean-travel-agent"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def build_standalone_app(require_auth: bool = True) -> FastAPI:
    """组装单端口应用。

    ``require_auth=False`` 给桌面版用：那边只监听 127.0.0.1，密码不是防线而是
    一堵用户打不开的墙——``WEB_AUTH_PASS`` 是给「对外暴露一个端口」的场景准备的，
    嵌在窗口里的浏览器没法先跟他要一次 Basic Auth。
    """
    app = create_app()
    user, password = _credentials()

    if require_auth and password:
        app.add_middleware(BasicAuthMiddleware, user=user, password=password)
    elif require_auth:
        logger.warning("WEB_AUTH_PASS 未配置：接口对外完全开放，任何人都会消耗模型额度")
    elif password:
        logger.warning("桌面版忽略 WEB_AUTH_PASS：服务只监听 127.0.0.1，不需要登录")

    # 挂载必须放在所有路由之后：Starlette 按注册顺序匹配，
    # /api/v1 和 /share 先注册就优先命中，剩下的才落到静态文件
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def __getattr__(name: str) -> Any:
    """``uvicorn app.standalone:app`` 用字符串导入，走的是模块属性访问。

    改成按需构建是为了桌面入口：它只要 ``build_standalone_app``，如果这里在
    import 时就建一个应用，进程里会同时存在两个——启动日志打两遍「key 来源」，
    ``configure_tracing`` 也会挂两套回调。重复初始化追踪比多打一行日志难查得多。

    构建结果写回模块全局，uvicorn 之后每次取都拿到同一个对象。
    """
    if name != "app":
        raise AttributeError(name)
    built = build_standalone_app()
    globals()["app"] = built
    return built
