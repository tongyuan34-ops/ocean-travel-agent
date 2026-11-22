"""FastAPI 入口。

启动：
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.permissions import build_decision_channel
from app.api.routes import (
    chat,
    media,
    memory,
    permissions,
    share,
    skills,
    system,
    tools,
    trips,
)
from app.api.routes import (
    metrics as metrics_routes,
)
from app.core import secrets
from app.core.config import KEY_NAMES, PROJECT_ROOT, load_settings
from app.core.errors import register_exception_handlers
from app.core.identity import TenantTokenMiddleware
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.security import register_root
from app.core.tracing import configure_langfuse, configure_tracing, shutdown_tracing
from app.mcp.registry import load_from_settings
from app.mcp.registry import shutdown as shutdown_mcp
from app.media.service import build_media_service
from app.media.uploads import UploadStore
from app.services.chat import ChatService
from app.services.profile import build_profile_service
from app.services.sessions import SessionService
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
    # Langfuse 与 LangSmith 互不影响，可以同时开。没配 key 时这里只返回 False
    configure_langfuse(settings)

    # 只打来源不打值。用户报「用不了」时，第一件事就是看他的 key 是哪来的
    _sources = secrets.describe(KEY_NAMES)
    logger.info(
        "key 来源：%s",
        "，".join(f"{name}={source or '未配置'}" for name, source in _sources.items()),
    )
    if any(source == secrets.BUNDLED for source in _sources.values()):
        logger.warning("正在使用**打包内置**的 key：额度用尽后请在 config.json 里填自己的")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.media = build_media_service(settings)
        # 用户上传的附件：跟 data/ 一起落盘，不进 git（见 .gitignore）
        uploads_root = PROJECT_ROOT / "data" / "uploads"
        app.state.uploads = UploadStore(uploads_root)
        # 上传目录要能被 read_document / query_sheet 读到，否则用户传完一份表格、
        # 模型要统计时会被「先授权这个目录」拦一下——而这个目录是程序自己建的，
        # 问用户毫无意义。注册成进程内只读根目录，绕开那次询问
        register_root(uploads_root)
        app.state.session_store = build_session_store(settings)
        # 会话目录（增删查/列表/风格与技能）单独成服务：路由列个会话列表时
        # 不该顺手构造一个会发起模型调用的 ChatService
        app.state.sessions = SessionService(app.state.session_store)
        app.state.profile = build_profile_service(settings)
        # 事件流与权限裁决：单进程用内存，多进程用 Redis（见 app/services/streams.py）
        app.state.event_log = build_event_log(settings)
        app.state.decisions = build_decision_channel(settings)
        app.state.chat = ChatService(
            settings,
            app.state.sessions,
            app.state.media,
            app.state.profile,
            uploads=app.state.uploads,
            event_log=app.state.event_log,
            decision_channel=app.state.decisions,
        )

        # 外部 MCP 工具：配置为空时什么都不做（返回 0），所以默认零成本。
        # 连不上的服务端只记日志、不阻断启动——可选集成不该让整个应用起不来
        external = await load_from_settings(settings)
        if external:
            logger.info("已接入 %d 个外部 MCP 工具", external)

        # 技能在第一次对话时才加载也来得及，但放在启动时能把「目录/工具加载失败」
        # 提前暴露在日志里，而不是等用户点了某个技能才看到一句异常
        from app.skills import reload as reload_skills

        loaded = reload_skills()
        if loaded:
            logger.info(
                "已加载 %d 个技能：%s",
                len(loaded),
                "、".join(f"{item.key}({len(item.tools)} 工具)" for item in loaded),
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
        # 外部 MCP 服务端是子进程，不显式断开就会留成孤儿进程
        await shutdown_mcp()
        # 追踪事件是后台批量上报的，不等它刷完就退出，最后几轮对话会丢在队列里
        shutdown_tracing()

    app = FastAPI(
        title="旅行规划助手后端",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(TenantTokenMiddleware, tokens=settings.tenant_tokens)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 最后加 = 最外层：连鉴权失败（401）也要计数，否则「突然所有请求都 401」
    # 这种事故在看板上会显示为「流量归零」，方向完全反了
    app.add_middleware(MetricsMiddleware)

    register_exception_handlers(app)

    app.include_router(system.meta_router, prefix=API_PREFIX)
    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(metrics_routes.router, prefix=API_PREFIX)
    # Prometheus 抓取路径按惯例放在根路径，不挂 /api/v1
    app.include_router(metrics_routes.text_router)
    app.include_router(chat.router, prefix=API_PREFIX)
    app.include_router(memory.router, prefix=API_PREFIX)
    app.include_router(skills.router, prefix=API_PREFIX)
    app.include_router(tools.router, prefix=API_PREFIX)
    app.include_router(permissions.router, prefix=API_PREFIX)
    app.include_router(media.router, prefix=API_PREFIX)
    app.include_router(trips.router, prefix=API_PREFIX)
    # 分享页不在 /api/v1 下，方便直接对外给只读链接
    app.include_router(share.router)

    return app


def __getattr__(name: str) -> Any:
    """``uvicorn app.main:app`` 用字符串导入，走的是模块属性访问。

    按需构建的理由和 ``app/standalone.py`` 里那条一样：这个模块的
    ``create_app`` 会被别人 import（standalone 就是），如果这里在 import 时
    也建一个，进程里就有两个应用——启动日志打两遍、``configure_tracing``
    挂两套回调。后者不会报错，只会让追踪数据重复上报，查起来很费劲。

    构建结果写回模块全局，之后每次取都拿到同一个对象。
    """
    if name != "app":
        raise AttributeError(name)
    built = create_app()
    globals()["app"] = built
    return built
