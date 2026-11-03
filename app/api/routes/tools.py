"""工具清单路由，对齐 SPEC 契约 2.2 的 GET /api/v1/tools。

前端据此渲染可用能力，并对 requires_permission=true 的工具给出提示，
因此这里的 `enabled` 必须反映**当前真实可用性**——
把跑不通的工具标成可用，前端就会放出一个必然失败的入口。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.agent.guard import DANGEROUS_TOOLS
from app.core.config import load_settings
from app.core.security import allowed_roots
from app.mcp.registry import all_tools
from app.schemas.tools import ToolItem, ToolList
from app.services.chat import tool_category

router = APIRouter(prefix="/tools", tags=["tools"])

# 有副作用、必须先经用户确认的工具（契约 2.4 的动作清单）。
# 直接复用注入防护里的那份：两处各写一遍迟早会漂移，
# 而漂移的后果是「接口说不需要确认，实际执行时却弹窗」或者反过来——
# 后者更糟：接口说不确认，污点闸也没盖到。
_PERMISSION_TOOLS = DANGEROUS_TOOLS


def _enabled(name: str, settings, media_caps: dict[str, Any]) -> bool:
    """工具当前是否真的能用。

    - 天气/路线/POI：需要地图 Key
    - 生图：需要媒体后端就绪（与 capabilities.image_gen 同源，不得两处不一致）
    - 社媒采集：只反映功能开关，具体平台由 capabilities.platforms 给出
    """
    if name in {
        "get_weather",
        "plan_route",
        "search_poi",
        "recommend_stay",
        "recommend_transport",
        "plan_trip",
        "estimate_budget",
        "optimize_day_route",
    }:
        return bool(settings.map_api_key or settings.weather_api_key)
    if name == "search_web":
        return True
    if name in {"fetch_url", "parse_shared_link"}:
        return True
    if name == "generate_image":
        return bool(media_caps.get("image_gen"))
    if name == "list_image_templates":
        return True
    if name in {"search_social_session", "generate_scraper"}:
        # 只反映"功能开关"；具体哪些平台可用由 capabilities 的 search_social 与
        # platforms 决定，避免这里出现"工具可用但一个平台都没配"的误导
        return settings.enable_session_fetch
    if name == "search_knowledge":
        # 语料随仓库分发，只要进程能起来就能查；语料为空时工具自己返回 KNOWLEDGE_EMPTY
        return True
    if name.startswith("mcp__"):
        # 外部工具能出现在清单里，说明连接已经成功（注册表只注册连上的）
        return True
    if name.startswith(("list_dir", "read_file")) or name in _PERMISSION_TOOLS:
        # 配置里的白名单 + 用户在使用中授权过的目录。都为空时文件工具不可用，
        # 但第一次真正需要时工具会自己弹授权，所以这里只是「当前是否已开」
        return bool(allowed_roots(settings))
    return True


def _description(tool) -> str:
    """取工具的中文说明首句，避免把长 docstring 整段塞给前端。"""
    text = (getattr(tool, "description", "") or "").strip()
    first = text.split("\n", 1)[0].strip()
    return first or text


@router.get("", response_model=ToolList)
async def list_tools(request: Request) -> ToolList:
    """返回工具清单。category 与 SSE tool_call 事件里的取值保持一致。"""
    settings = load_settings()
    media_caps = request.app.state.media.capabilities()
    items = [
        ToolItem(
            name=tool.name,
            category=tool_category(tool.name),
            enabled=_enabled(tool.name, settings, media_caps),
            requires_permission=tool.name in _PERMISSION_TOOLS,
            description=_description(tool),
        )
        for tool in all_tools()
    ]
    return ToolList(items=items)
