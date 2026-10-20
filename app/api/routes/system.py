"""系统信息路由：能力探测与健康检查，对齐 SPEC 契约 2.6 / 2.7。

capabilities 只反映**当前真实可用**的能力，一律不得谎报——
前端据此隐藏入口，谎报会让用户点到必然失败的按钮。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from typing import Any

from app.adapters import available_platforms
from app.adapters.mediacrawler import SOCIAL_PLATFORM_KEYS
from app.core.config import Settings
from app.core.security import is_windows
from app.version import __version__

router = APIRouter(prefix="/system", tags=["system"])
meta_router = APIRouter(tags=["system"])


def _platforms(settings: Settings) -> list[str]:
    """数据源清单：适配器注册表中 available()=True 的源，外加已配置的高德/天气。

    高德（天气/路线/POI 共用同一个 Web 服务 Key）与天气不是适配器层实现，
    但前端要展示「数据来源」标签，所以在这里一并列出。
    """
    names = list(available_platforms())
    if settings.map_api_key or settings.weather_api_key:
        if "amap" not in names:
            names.insert(0, "amap")
        if "weather" not in names:
            names.insert(1, "weather")
    return names


@router.get("/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    media_caps = request.app.state.media.capabilities()
    settings = request.app.state.settings
    platforms = _platforms(settings)
    social_ready = [name for name in platforms if name in SOCIAL_PLATFORM_KEYS]

    return {
        "image_gen": media_caps["image_gen"],
        "video_gen": media_caps["video_gen"],
        # system 组：沙箱与壁纸
        "filesystem": bool(settings.allowed_roots),
        "wallpaper": is_windows(),
        # plugin 组 A：用户粘贴的分享链接解析，默认启用
        "link_parse": True,
        # search 组：必应可用即代表通用网页检索可用
        "search_web": "bing_cn" in platforms,
        # plugin 组 B（登录态采集）：默认关闭。
        # search_social 只在"开关已开 + Cookie 已配 + MediaCrawler 已就位"时才为 true，
        # 否则前端会放出一个必然失败的入口
        "search_social": bool(social_ready),
        "session_fetch": settings.enable_session_fetch,
        # 前端据此渲染来源筛选器，不得硬编码平台清单
        "platforms": platforms,
    }


@meta_router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "version": __version__,
        "model": settings.model_name,
        "provider": settings.model_provider,
    }
