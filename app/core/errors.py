"""统一异常与错误码映射。

把各类异常收敛成 SPEC 第二部分 2.3 约定的 {code, message} 结构，
避免错误形状在各路由里各不相同。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.media.base import MediaError

logger = logging.getLogger(__name__)

# 机器可读错误码 → HTTP 状态码（清单与 SPEC 2.3 保持一致）
CODE_TO_HTTP: dict[str, int] = {
    "INVALID_REQUEST": 400,
    "PERMISSION_DENIED": 403,
    "PATH_NOT_ALLOWED": 403,
    "SESSION_NOT_FOUND": 404,
    "TASK_NOT_FOUND": 404,
    "VALIDATION_ERROR": 422,
    "RATE_LIMITED": 429,
    "TOOL_NOT_ENABLED": 501,
    "MODEL_UNAVAILABLE": 502,
    "MODEL_EMPTY_RESPONSE": 502,
    "MEDIA_UPSTREAM_ERROR": 502,
    "MEDIA_BACKEND_MISSING": 503,
    "SESSION_EXPIRED": 503,
    "MODEL_TIMEOUT": 504,
    "MEDIA_UPSTREAM_TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}


class ApiError(Exception):
    """业务异常，可直接指定错误码。"""

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status or CODE_TO_HTTP.get(code, 500)


def _payload(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message),
        )

    @app.exception_handler(MediaError)
    async def _handle_media_error(_: Request, exc: MediaError) -> JSONResponse:
        # 媒体层抛出的错误码直接透传，不包装成泛化错误
        status = CODE_TO_HTTP.get(exc.code, 502)
        return JSONResponse(status_code=status, content=_payload(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = str(exc.errors())[:300]
        return JSONResponse(status_code=422, content=_payload("VALIDATION_ERROR", detail))

    @app.exception_handler(Exception)
    async def _handle_unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s", exc)
        return JSONResponse(status_code=500, content=_payload("INTERNAL_ERROR", "服务器内部错误"))
