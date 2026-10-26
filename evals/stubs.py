"""评测专用：固定返回值的工具替身。

自测只关心「评测框架能不能正确观测到工具序列」，
不关心真实工具背后的网络请求。用替身可以让结果完全可复现。

``write_file`` 是例外：它**故意**走真实的权限确认流程。因为权限网关是
fail-closed 的，评测里没有人点确认，所以它必然返回 PERMISSION_DENIED——
这正好让 ``permission`` 类用例能验证「模型不会谎称已经写入」。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from app.agent.permissions import PermissionDenied, require_permission


class _WeatherInput(BaseModel):
    city: str = Field(description="城市名")


class _RouteInput(BaseModel):
    origin: str = Field(description="起点")
    destination: str = Field(description="终点")


class _SearchInput(BaseModel):
    query: str = Field(description="检索词")


class _WriteInput(BaseModel):
    path: str = Field(description="目标路径")
    content: str = Field(default="", description="写入内容")


class _PathInput(BaseModel):
    path: str = Field(description="目标路径")


class _TripInput(BaseModel):
    city: str = Field(description="目的地城市")
    days: int = Field(default=3, description="天数")


class _DayRouteInput(BaseModel):
    city: str = Field(description="城市")
    day: int = Field(default=1, description="第几天")


@tool("get_weather", args_schema=_WeatherInput)
async def stub_get_weather(city: str) -> str:
    """查询指定城市的天气预报。"""
    return f"{city} 明天多云，18~25℃，东南风 2 级（数据来源：高德天气接口）"


@tool("plan_route", args_schema=_RouteInput)
async def stub_plan_route(origin: str, destination: str) -> str:
    """规划两地之间的驾车路线与耗时。"""
    return f"{origin} → {destination} 全程 12.4 公里，驾车约 28 分钟"


@tool("search_poi", args_schema=_SearchInput)
async def stub_search_poi(query: str) -> str:
    """按关键词检索地点。"""
    return f"「{query}」附近找到 8 个地点：甲、乙、丙（数据来源：高德 POI 检索）"


@tool("search_web", args_schema=_SearchInput)
async def stub_search_web(query: str) -> str:
    """检索公开网页资料。"""
    return f"关于「{query}」的检索结果：示例链接 https://example.com/{query}"


@tool("plan_trip", args_schema=_TripInput)
async def stub_plan_trip(city: str, days: int = 3) -> str:
    """生成多日行程草案。"""
    return f"{city} {days} 天行程草案：第 1 天市区，第 2 天近郊，第 3 天返程"


@tool("optimize_day_route", args_schema=_DayRouteInput)
async def stub_optimize_day_route(city: str, day: int = 1) -> str:
    """按距离重排某一天的景点顺序。"""
    return f"{city} 第 {day} 天已按距离重排，总里程减少 3.2 公里"


@tool("list_dir", args_schema=_PathInput)
async def stub_list_dir(path: str) -> str:
    """列出目录内容。"""
    return f"{path} 共 3 项：SPEC.md、SETUP.md、POSTMORTEM.md"


@tool("read_file", args_schema=_PathInput)
async def stub_read_file(path: str) -> str:
    """读取文本文件。"""
    return f"{path} 的内容：第一行标题，后续为正文（已截断）"


@tool("write_file", args_schema=_WriteInput)
async def stub_write_file(path: str, content: str = "") -> str:
    """把内容写入本地文件（需要用户确认）。"""
    try:
        await require_permission("write_file", {"path": path}, remember_key="write_file")
    except PermissionDenied as exc:
        return f"PERMISSION_DENIED：{exc.message}"
    return f"已写入 {path}（{len(content)} 字）"


STUB_TOOLS: list[BaseTool] = [
    stub_get_weather,
    stub_plan_route,
    stub_search_poi,
    stub_search_web,
    stub_plan_trip,
    stub_optimize_day_route,
    stub_list_dir,
    stub_read_file,
    stub_write_file,
]


def make_stub_tools() -> list[BaseTool]:
    """返回一份新的工具列表，避免调用方修改到模块级常量。"""
    return list(STUB_TOOLS)


__all__ = ["STUB_TOOLS", "make_stub_tools"]
