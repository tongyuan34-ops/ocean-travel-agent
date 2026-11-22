"""工具注册表。

按 SPEC 第八节分组。已完成 info / plan / system / search / knowledge / plugin（A+B）/
media / delivery 组。
生视频不在范围内（产品决定），故 media 组只注册生图相关工具。
"""

from __future__ import annotations

from app.agent.tools.delivery import DELIVERY_TOOLS, export_trip_pdf, render_trip_map
from app.agent.tools.docs import (
    DOCS_TOOLS,
    aggregate_sheet,
    digest_documents,
    edit_document,
    organize_documents,
    query_sheet,
    read_document,
    write_sheet,
)
from app.agent.tools.info import get_weather, plan_route, search_poi
from app.agent.tools.knowledge import search_knowledge
from app.agent.tools.media import generate_image, list_image_templates
from app.agent.tools.meta import discover_tools
from app.agent.tools.plan import estimate_budget, optimize_day_route
from app.agent.tools.plugin import parse_shared_link
from app.agent.tools.search import fetch_url, search_web
from app.agent.tools.skills import use_skill
from app.agent.tools.social import generate_scraper, search_social_session
from app.agent.tools.stay import recommend_stay
from app.agent.tools.system import (
    create_dir,
    delete_path,
    list_dir,
    open_file,
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
SYSTEM_TOOLS = [
    list_dir,
    read_file,
    open_file,
    write_file,
    create_dir,
    delete_path,
    set_wallpaper,
]
SEARCH_TOOLS = [search_web, fetch_url]
# 知识库检索单独成组：它查的是「规则」，search 组查的是「实时事实」，
# 两者在提示词里承担不同的举证责任，混在一组会让模型分不清
KNOWLEDGE_TOOLS = [search_knowledge]
PLUGIN_TOOLS = [parse_shared_link, search_social_session, generate_scraper]
MEDIA_TOOLS = [generate_image, list_image_templates]

# 元工具：不在任何能力组里，每轮常驻。工具是按轮路由的（见 app/agent/toolset.py），
# 模型手上永远只有一小撮工具，没有它们就没法在关键词漏网时自己申请能力或技能。
# 两个都是「只读说明 + 激活」：真正的业务动作仍走各自被绑定的工具。
META_TOOLS = [discover_tools, use_skill]

ALL_TOOLS = [
    *META_TOOLS,
    *DELIVERY_TOOLS,
    *INFO_TOOLS,
    *PLAN_TOOLS,
    *SYSTEM_TOOLS,
    *DOCS_TOOLS,
    *SEARCH_TOOLS,
    *KNOWLEDGE_TOOLS,
    *PLUGIN_TOOLS,
    *MEDIA_TOOLS,
]

__all__ = [
    "ALL_TOOLS",
    "DELIVERY_TOOLS",
    "DOCS_TOOLS",
    "INFO_TOOLS",
    "KNOWLEDGE_TOOLS",
    "MEDIA_TOOLS",
    "META_TOOLS",
    "PLAN_TOOLS",
    "PLUGIN_TOOLS",
    "SEARCH_TOOLS",
    "SYSTEM_TOOLS",
    "aggregate_sheet",
    "create_dir",
    "delete_path",
    "digest_documents",
    "discover_tools",
    "edit_document",
    "estimate_budget",
    "export_trip_pdf",
    "fetch_url",
    "generate_image",
    "generate_scraper",
    "get_weather",
    "list_dir",
    "list_image_templates",
    "open_file",
    "optimize_day_route",
    "organize_documents",
    "parse_shared_link",
    "plan_route",
    "plan_trip",
    "query_sheet",
    "read_document",
    "read_file",
    "recommend_stay",
    "recommend_transport",
    "render_trip_map",
    "search_knowledge",
    "search_poi",
    "search_social_session",
    "search_web",
    "set_wallpaper",
    "use_skill",
    "write_file",
    "write_sheet",
]
