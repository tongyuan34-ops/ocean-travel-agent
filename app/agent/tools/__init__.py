"""工具注册表。

按 SPEC 第八节分组。已完成 info / plan / system / search / plugin（A+B）/ media 组。
生视频不在范围内（产品决定），故 media 组只注册生图相关工具。
"""

from __future__ import annotations

from app.agent.tools.info import get_weather, plan_route, search_poi
from app.agent.tools.media import generate_image, list_image_templates
from app.agent.tools.plan import estimate_budget, optimize_day_route
from app.agent.tools.plugin import parse_shared_link
from app.agent.tools.search import fetch_url, search_web
from app.agent.tools.social import generate_scraper, search_social_session
from app.agent.tools.stay import recommend_stay
from app.agent.tools.system import (
    create_dir,
    delete_path,
    list_dir,
    read_file,
    set_wallpaper,
    write_file,
)
from app.agent.tools.transport import recommend_transport
from app.agent.tools.trip import plan_trip

INFO_TOOLS = [get_weather, plan_route, search_poi]
PLAN_TOOLS = [
    estimate_budget,
    optimize_day_route,
    plan_trip,
    recommend_stay,
    recommend_transport,
]
SYSTEM_TOOLS = [list_dir, read_file, write_file, create_dir, delete_path, set_wallpaper]
SEARCH_TOOLS = [search_web, fetch_url]
PLUGIN_TOOLS = [parse_shared_link, search_social_session, generate_scraper]
MEDIA_TOOLS = [generate_image, list_image_templates]

ALL_TOOLS = [
    *INFO_TOOLS,
    *PLAN_TOOLS,
    *SYSTEM_TOOLS,
    *SEARCH_TOOLS,
    *PLUGIN_TOOLS,
    *MEDIA_TOOLS,
]

__all__ = [
    "ALL_TOOLS",
    "INFO_TOOLS",
    "MEDIA_TOOLS",
    "PLAN_TOOLS",
    "PLUGIN_TOOLS",
    "SEARCH_TOOLS",
    "SYSTEM_TOOLS",
    "create_dir",
    "delete_path",
    "estimate_budget",
    "fetch_url",
    "generate_image",
    "generate_scraper",
    "get_weather",
    "list_dir",
    "list_image_templates",
    "optimize_day_route",
    "parse_shared_link",
    "plan_route",
    "plan_trip",
    "read_file",
    "recommend_stay",
    "recommend_transport",
    "search_poi",
    "search_social_session",
    "search_web",
    "set_wallpaper",
    "write_file",
]
