"""行程交付：把排好的行程变成 A4 PDF，或一张按顺序的路线图。

**为什么要有这两个工具**

导出原本只有 HTTP 接口，只能由前端的「导出」按钮触发。用户说「帮我出一份
PDF」时，助手手里没有工具，只能回一句「请点上面的导出按钮」——话说到一半，
而且用户已经明确要求了，还要他自己去点。

工具补上的就是这一步：助手能真的把文件做出来，再把链接给用户。

**为什么参数是「每天一串地名」**

助手刚排完行程，手上只有这些。要它再填时刻、备注、里程，只会多出编造的机会；
这些字段留空，PDF 里就是干净的地点列表，和它在正文里说的对得上。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

# 服务层的导入放在函数里：``app.services.trip_export`` → ``app.services.trips``
# → ``app.agent.tools.info`` → 本包的 ``__init__`` → 本模块，顶层导入会绕成环。
# 这条链只有在「先 import app.services」时才暴露，线上刚好相反顺序才没炸。

# 链接指向 GET /trips/files/{name}；前端按这个前缀把它渲染成可点的卡片
FILE_URL = "/trips/files"


class _DayInput(BaseModel):
    """一天的安排。"""

    date: str = Field(default="", description="YYYY-MM-DD；不确定就留空，PDF 里按天序排")
    title: str = Field(default="", description="当天主题，如「亚龙湾 + 蜈支洲岛」")
    stops: list[str] = Field(
        default_factory=list, description="当天按游玩顺序排列的地点名，2~6 个为宜"
    )


class ExportTripPdfInput(BaseModel):
    days: list[_DayInput] = Field(description="逐天的地点安排，按天顺序排列")
    title: str = Field(default="", description="行程标题，如「三亚 4 天亲子」")
    city: str = Field(default="", description="目的地城市，用于地名消歧")
    origin: str = Field(default="", description="出发地，如「三亚凤凰机场」")
    start_date: str = Field(default="", description="出发日期 YYYY-MM-DD，用户没给就留空")
    mode: str = Field(default="driving", description="driving / walking / bicycling")


class RenderTripMapInput(BaseModel):
    stops: list[str] = Field(description="按顺序排列的地点名，地图上会从 1 开始编号连线")
    city: str = Field(default="", description="目的地城市，用于地名消歧")
    origin: str = Field(default="", description="起点，可不填")


def _url(path: Path) -> str:
    return f"{FILE_URL}/{path.name}"


def _describe_stops(names: list[str], limit: int = 6) -> str:
    shown = names[:limit]
    text = " → ".join(shown)
    return text + (f" 等 {len(names)} 个地点" if len(names) > limit else "")


@tool(args_schema=ExportTripPdfInput)
async def export_trip_pdf(
    days: list[_DayInput],
    title: str = "",
    city: str = "",
    origin: str = "",
    start_date: str = "",
    mode: str = "driving",
) -> str:
    """把行程排成 A4 PDF 行程单（含总览地图与每日地图）。

    **只在用户明确同意之后调用。** 行程刚讲完时应该先问一句「要不要整理成
    A4 PDF」，用户说要，再调这个工具。

    **调用前不要重排**：不要为了「确认一下」再调 plan_trip / search_poi /
    get_weather。上文给用户看的那份就是最终版，地点名照抄，直接填进来。
    重排会挑出另一批景点，用户拿到的 PDF 和刚才看到的方案对不上。
    """
    from app.services.trip_export import build_trip_pdf, from_day_names, normalize

    plan = from_day_names(
        title=title,
        city=city,
        origin=origin,
        mode=mode,
        start_date=start_date,
        days=[(day.date, day.title, day.stops) for day in days],
    )
    plan = normalize(plan)
    total = sum(len(day.stops) for day in plan.days)
    if total == 0:
        return "INVALID_REQUEST：没有任何地点，先把每天的安排排出来再生成 PDF。"

    try:
        path = await build_trip_pdf(plan)
    except Exception as exc:  # noqa: BLE001 - 渲染失败要如实说，不能假装成功
        return f"PDF_FAILED：{exc}"

    lines = [
        f"PDF 已生成：{_url(path)}",
        f"{plan.title} · {len(plan.days)} 天 · {total} 个地点 · A4 含每日地图",
        "把上面这个链接原样给用户（页面会渲染成可点的卡片），"
        "并说明地图没取到的日子会只保留文字安排。",
    ]
    return "\n".join(lines)


@tool(args_schema=RenderTripMapInput)
async def render_trip_map(stops: list[str], city: str = "", origin: str = "") -> str:
    """按地点顺序生成一张静态路线图（带编号与连线）。

    **只在用户明确同意之后调用。** 它和 PDF 是两个选项，不要一次都做掉——
    用户可能只要其中一样。

    **调用前不要重排**：地点名照抄上文给过用户的那份，不要再调 plan_trip
    或 search_poi 重新挑点。
    """
    from app.services.trips import TripMapError, build_map

    names = [name.strip() for name in stops if name and name.strip()]
    if not names:
        return "INVALID_REQUEST：至少给一个地点。"

    try:
        path = await build_map(stops=names, city=city, origin=origin)
    except TripMapError as exc:
        return f"{exc.code}：{exc.message}"
    except Exception as exc:  # noqa: BLE001
        return f"MAP_FAILED：{exc}"

    return "\n".join(
        [
            f"路线图已生成：{_url(path)}",
            f"按顺序：{_describe_stops(names)}",
            "把上面这个链接原样给用户（页面会直接显示这张图）。",
        ]
    )


# 供 __init__ 注册；单独列出来是为了让「交付」这一组在提示词里能整体引用
DELIVERY_TOOLS = [export_trip_pdf, render_trip_map]
