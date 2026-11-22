"""请求身份解析：设备维度 + 租户维度。

**两种模式**

1. 配了 ``TENANT_TOKENS``（生产/对外）：请求必须带
   ``Authorization: Bearer <令牌>``，令牌决定租户。请求头里的
   ``X-Tenant-ID`` 一律不信——那个只是「自报家门」，任何人写什么就是什么。
2. 没配（桌面版、本地开发、脚本）：保持旧行为，按 ``X-Device-ID`` /
   ``X-Tenant-ID`` 区分，缺失落到 ``default``，零配置可用。

中间件只负责把令牌换成 ``scope["state"]["tenant"]``；判定仍然放在这两个
函数里，路由层不需要知道当前是哪种模式。

**为什么用户维度和租户维度要合并**：令牌模式下 ``resolve_user_id`` 直接返回
租户。否则会出现「会话属于 acme，而长期偏好属于某个设备 ID」这种对不上的
状态，删除数据、导出数据都没法按一个维度圈定范围。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

DEFAULT_USER_ID = "default"
DEFAULT_TENANT = "default"
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# 租户 ID 常是短名（acme、team-1），不能套设备 ID 的 8 位下限
_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 不需要令牌就能访问的路径：健康检查（容器探针）、能力探测（前端启动时拉）、
# 分享页（凭 URL 里的 token）以及 OpenAPI 文档
_PUBLIC_PATHS = frozenset(
    {"/api/v1/health", "/api/v1/system/capabilities", "/docs", "/openapi.json", "/redoc"}
)
_PUBLIC_PREFIXES = ("/share/",)
# 只放开 GET：``<img src>`` 带不了 Authorization 头，原图只能靠「id 不可猜」
# 保护（和旧行为一致）。同路径的 DELETE 仍然要令牌，否则等于把删除也公开了
_PUBLIC_GET_PREFIXES = ("/api/v1/media/uploads/",)


def tenant_of(request: Request) -> str | None:
    """已经通过令牌认证的租户；没有则为 None。"""
    state = request.scope.get("state") or {}
    value = state.get("tenant")
    return value if isinstance(value, str) and value else None


def resolve_user_id(request: Request) -> str:
    """用户维度。

    令牌模式下用户就是租户（见模块开头的说明）；否则退回设备 ID。
    """
    authenticated = tenant_of(request)
    if authenticated is not None:
        return authenticated
    raw = (request.headers.get("X-Device-ID") or "").strip()
    return raw if _DEVICE_RE.match(raw) else DEFAULT_USER_ID


def resolve_tenant_id(request: Request) -> str:
    """租户维度：认证过的身份优先，其次请求头，最后 default。

    不抛错而是回落：没配令牌时现有的桌面版、命令行脚本都不带这个头，
    让它们继续以默认租户的身份照常工作，比统一 400 更有用。
    """
    authenticated = tenant_of(request)
    if authenticated is not None:
        return authenticated
    raw = (request.headers.get("X-Tenant-ID") or "").strip()
    return raw if _TENANT_RE.match(raw) else DEFAULT_TENANT


def _bearer_token(raw: str) -> str:
    scheme, _, value = raw.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


class TenantTokenMiddleware:
    """纯 ASGI：把 Bearer 令牌解析成租户写进 ``scope["state"]``。

    没配令牌时完全透传，桌面版不需要任何额外配置。配了令牌但令牌缺失或
    不认识时返回 401，而不是「当成 default 放过去」——默认租户里同样存着
    数据，静默降级等于把那些数据交出去。
    """

    def __init__(self, app: Any, tokens: dict[str, str]) -> None:
        self.app = app
        # 反查表：令牌 → 租户。值重复时后者覆盖前者，配置时别给两个租户同一个令牌
        self._tokens = {secret: tenant for tenant, secret in tokens.items() if secret}

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not self._tokens:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return
        if scope.get("method") == "GET" and path.startswith(_PUBLIC_GET_PREFIXES):
            await self.app(scope, receive, send)
            return

        token = _bearer_token(Headers(scope=scope).get("authorization", ""))
        tenant = self._tokens.get(token) if token else None
        if tenant is None:
            response = JSONResponse(
                {"detail": "缺少或无效的访问令牌"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["tenant"] = tenant
        await self.app(scope, receive, send)
