"""系统信息路由：能力探测与健康检查，对齐 SPEC 契约 2.6 / 2.7。

capabilities 只反映**当前真实可用**的能力，一律不得谎报——
前端据此隐藏入口，谎报会让用户点到必然失败的按钮。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.adapters import available_platforms
from app.adapters.mediacrawler import SOCIAL_PLATFORM_KEYS
from app.core import secrets
from app.core.config import KEY_NAMES, Settings
from app.core.paths import resolve_resource
from app.core.security import allowed_roots, is_windows
from app.rag import get_knowledge_base
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
    sources = secrets.describe(KEY_NAMES)
    on_bundled = sorted(name for name, source in sources.items() if source == secrets.BUNDLED)
    roots = allowed_roots(settings)

    return {
        "image_gen": media_caps["image_gen"],
        "video_gen": media_caps["video_gen"],
        # system 组：沙箱与壁纸。
        # 恒为 true：文件工具本身永远可用，能不能写某个目录由**按需授权**在
        # 那一次操作里问。按 allowed_roots 判空会让刚装好的机器显示「文件功能
        # 不可用」，而实际上它只是还没被问过——下面 allowed_roots 给的是
        # 「已经放开到哪儿」这个回执。
        "filesystem": True,
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
        # 图片输入：上传目录就绪即视为可用。
        # 模型是否真的看得懂由服务商决定，这里只报「链路通不通」——
        # 报一个假的 true 会让用户传完图才发现模型是纯文本的，那才是更糟的体验
        "vision": request.app.state.uploads is not None,
        # 前端据此渲染来源筛选器，不得硬编码平台清单
        "platforms": platforms,
        # 当前允许访问的目录（配置里的 + 用户按需授权过的）。
        # 报出来是为了让用户**看得见**范围——授权是渐进的，不给个回执的话
        # 用户不知道自己到底放开了多少。
        "allowed_roots": [str(root) for root in roots],
        # key 是从哪来的：env / config / bundled / ""（没配）。**只有来源，没有值**。
        # 用内置额度时前端该提示一句「额度用尽可自行填写」，否则用户会以为程序坏了
        "key_sources": sources,
        "using_bundled_keys": on_bundled,
    }


@meta_router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    # 语料片段数顺带报出来：打包版的语料在 _internal/ 里，路径解析错了会
    # 静默变成空库（工具返回 KNOWLEDGE_EMPTY），只查「文件在不在包里」看不出来。
    # 这里给一个运行时可验证的信号，冒烟测试据此断言。
    chunks = get_knowledge_base(resolve_resource(settings.knowledge_dir)).size
    return {
        "status": "ok",
        "version": __version__,
        "model": settings.model_name,
        "provider": settings.model_provider,
        "knowledge_chunks": chunks,
    }
