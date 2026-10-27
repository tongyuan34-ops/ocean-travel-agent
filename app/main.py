"""FastAPI 入口。

启动：
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.permissions import build_decision_channel
from app.api.routes import chat, media, memory, permissions, share, system, tools, trips
from app.core.config import PROJECT_ROOT, load_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.tracing import configure_tracing
from app.media.service import build_media_service
from app.media.uploads import UploadStore
from app.services.chat import ChatService
from app.services.profile import build_profile_service
from app.services.streams import build_event_log
from app.storage import build_session_store
from app.version import __version__

API_PREFIX = "/api/v1"
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = load_settings()

    # 日志与追踪必须在任何业务代码之前配好，否则启动期的告警拿不到 trace_id
    configure_logging(settings.log_level)
    configure_tracing(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.media = build_media_service(settings)
        # 用户上传的图片：跟 data/ 一起落盘，不进 git（见 .gitignore）
        app.state.uploads = UploadStore(PROJECT_ROOT / "data" / "uploads")
        app.state.session_store = build_session_store(settings)
        app.state.profile = build_profile_service(settings)
        # 事件流与权限裁决：单进程用内存，多进程用 Redis（见 app/services/streams.py）
        app.state.event_log = build_event_log(settings)
        app.state.decisions = build_decision_channel(settings)
        app.state.chat = ChatService(
            settings,
            app.state.session_store,
            app.state.media,
            app.state.profile,
            uploads=app.state.uploads,
            event_log=app.state.event_log,
            decision_channel=app.state.decisions,
        )

        # 启动时把不可用原因打出来，避免"配了但没生效"这类静默问题
        for kind, reason in app.state.media.unavailable_reasons().items():
            logger.warning("媒体后端不可用 | %s: %s", kind, reason)

        if not await app.state.session_store.ping():
            logger.error(
                "会话存储不可用：无法连接 %s，会话相关接口会失败。"
                "请启动 Redis，或把 STORAGE_BACKEND 改回 memory。",
                settings.redis_url,
            )

        yield

        # 等待进行中的媒体任务收尾，并释放存储连接
        await app.state.media.aclose()
        await app.state.profile.aclose()
        await app.state.event_log.aclose()
        await app.state.decisions.aclose()
        await app.state.session_store.aclose()

    app = FastAPI(
        title="旅行规划助手后端",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(system.meta_router, prefix=API_PREFIX)
    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(chat.router, prefix=API_PREFIX)
    app.include_router(memory.router, prefix=API_PREFIX)
    app.include_router(tools.router, prefix=API_PREFIX)
    app.include_router(permissions.router, prefix=API_PREFIX)
    app.include_router(media.router, prefix=API_PREFIX)
    app.include_router(trips.router, prefix=API_PREFIX)
    # 分享页不在 /api/v1 下，方便直接对外给只读链接
    app.include_router(share.router)

    return app


app = create_app()
