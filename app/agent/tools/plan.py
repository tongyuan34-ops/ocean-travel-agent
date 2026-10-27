"""plan 组工具：预算汇总与单日路线优化。

两者都是**确定性计算**，不依赖模型推理，也不需要新的数据源：
- estimate_budget：纯算术，避免模型把预算加错
- optimize_day_route：用真实坐标做最近邻排序，再调高德接口算实际总里程与耗时

关于 SPEC 原列的另外三个工具（recommend_stay / recommend_transport /
recommend_car_rental）：酒店房价、班次、租车报价都没有合规数据来源。
按项目底线「宁可说查不到也不编造」，这三个暂不实现为工具。
住宿等建议改由模型基于真实 POI 检索结果给出，并标注「需以官方渠道为准」。
"""

from __future__ import annotations

import math

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.tools.info import _amap_get, _amap_key, _geocode, _not_enabled
from app.core.context import truncate_tool_output

_EARTH_RADIUS_KM = 6371.0
# 单日在途耗时超过这个值就提示行程排得过满（分钟）
_DAY_TRAVEL_WARN_MIN = 150.0
# 超过这个距离认为地址解析出错
_MAX_PLAUSIBLE_KM = 500.0
# 高德 waypoints 上限
_MAX_WAYPOINTS = 16


# ---------- 预算 ----------


class BudgetItem(BaseModel):
    """一条预算明细。"""

    name: str = Field(description="项目名称，如 机票 / 酒店 / 门票")
    amount: float = Field(description="金额，单位元")
    category: str = Field(
        default="其他", description="分类：交通 / 住宿 / 餐饮 / 门票 / 购物 / 其他"
    )


class EstimateBudgetInput(BaseModel):
    items: list[BudgetItem] = Field(description="预算明细列表")
    budget_total: float = Field(default=0, description="总预算，单位元；0 表示未设定")
    people: int = Field(default=1, description="出行人数，用于计算人均")


@tool(args_schema=EstimateBudgetInput)
async def estimate_budget(items: list[BudgetItem], budget_total: float = 0, people: int = 1) -> str:
    """汇总旅行预算：分项合计、分类小计、占总预算比例、余额或超支、人均花费。

    只做算术，items 里的金额必须来自用户原话或工具查到的真实数据。

    **用户只问「大概要花多少钱」、一个金额都没给时，不要调用本工具**——
    要调用就得先自己编出机票酒店的价格，那正好违反「不编造价格」的底线。
    这种情况直接回答：说明拿不到实时价格、列出花费构成（交通/住宿/餐饮/门票）、
    提醒以平台实价为准。
    """
    if not items:
        return "未提供预算明细，无法计算。请先向用户收集各项开销。"

    total = sum(item.amount for item in items)
    by_category: dict[str, float] = {}

    lines = ["预算明细（金额来自用户提供，此处仅做算术汇总）："]
    for item in items:
        lines.append(f"- {item.name}：{item.amount:,.0f} 元（{item.category}）")
        by_category[item.category] = by_category.get(item.category, 0.0) + item.amount

    lines.append("\n分类小计：")
    for category, amount in sorted(by_category.items(), key=lambda kv: -kv[1]):
        percent = f"，占总预算 {amount / budget_total * 100:.1f}%" if budget_total > 0 else ""
        lines.append(f"- {category}：{amount:,.0f} 元{percent}")

    lines.append(f"\n合计：{total:,.0f} 元")
    if people > 0:
        lines.append(f"人均：{total / people:,.0f} 元")

    if budget_total > 0:
        diff = budget_total - total
        if diff >= 0:
            lines.append(f"总预算 {budget_total:,.0f} 元，剩余 {diff:,.0f} 元")
        else:
            lines.append(f"总预算 {budget_total:,.0f} 元，超支 {-diff:,.0f} 元")

    return truncate_tool_output("\n".join(lines))


# ---------- 单日路线 ----------


class OptimizeRouteInput(BaseModel):
    city: str = Field(description="所在城市，如 三亚。必须提供，否则模糊地名会解析到错误位置")
    places: list[str] = Field(
        description="当天要去的地点名称，至少两个，如 [亚龙湾国家旅游度假区, 蜈支洲岛码头]"
    )
    mode: str = Field(
        default="driving", description="driving（驾车）/ walking（步行）/ bicycling（骑行）"
    )
    start: str = Field(default="", description="当天出发点，如酒店名；留空则从地点列表第一个开始")


@tool(args_schema=OptimizeRouteInput)
async def optimize_day_route(
    city: str, places: list[str], mode: str = "driving", start: str = ""
) -> str:
    """把**已经确定的一天内多个地点**排成少走回头路的顺序，并算出实际总里程与在途耗时。

    只在「地点定了、顺序没定」时用它：典型说法是「这几个地方怎么排顺序」
    「第一天先去哪个」「怎么走不绕路」。

    用户还在问「N 天怎么玩」「帮我规划行程」时**不要**用它——
    那时应该先用 plan_trip 出整体线路，再回来优化其中某一天。
    只有一天且地点也没定的，先用 search_poi 拿到具体地点再进来。

    用真实坐标做最近邻排序，再用高德路线接口计算实际里程与耗时，
    可用于判断某天行程是否排得过满。不返回任何票价或班次信息。
    """
    if not _amap_key():
        return _not_enabled("optimize_day_route")

    names = [name.strip() for name in places if name and name.strip()]
    if len(names) < 2:
        return "至少需要两个地点才能做路线排序。"

    resolved: list[tuple[str, tuple[float, float]]] = []
    unresolved: list[str] = []
    for name in names:
        located = await _geocode(name, city or None)
        if located is None:
            unresolved.append(name)
            continue
        coord = _parse_location(located[0])
        if coord is None:
            unresolved.append(name)
            continue
        resolved.append((name, coord))

    if len(resolved) < 2:
        return (
            f"无法解析以下地点：{'、'.join(unresolved) or '全部'}。"
            "请改用更具体的地名（景点全称）后重试。"
        )

    anchor: tuple[float, float] | None = None
    if start.strip():
        located_start = await _geocode(start.strip(), city or None)
        if located_start is not None:
            anchor = _parse_location(located_start[0])

    ordered = _nearest_neighbor_order(resolved, anchor)

    if mode not in {"driving", "walking", "bicycling"}:
        mode = "driving"

    origin = ordered[0][1]
    destination = ordered[-1][1]
    params = {
        "key": _amap_key(),
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{destination[0]},{destination[1]}",
    }
    middle = [coord for _, coord in ordered[1:-1]][:_MAX_WAYPOINTS]
    if middle:
        params["waypoints"] = ";".join(f"{lng},{lat}" for lng, lat in middle)

    data = await _amap_get(f"/v3/direction/{mode}", params)
    if data.get("status") != "1":
        return f"路线计算失败：{data.get('info') or '未知原因'}"

    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        return "未能为这些地点找到可用路线。"

    path = paths[0]
    distance_km = int(path.get("distance") or 0) / 1000
    duration_min = int(path.get("duration") or 0) / 60

    if distance_km > _MAX_PLAUSIBLE_KM:
        return (
            f"路线数据异常：总里程 {distance_km:.0f} 公里，明显不符合常规出行距离，"
            "该结果不可采用。通常是个别地名被解析到了错误位置，"
            "请改用更具体的地名重试。"
        )

    lines = [
        f"单日路线优化（{city}，{mode}）",
        "建议顺序：" + " → ".join(name for name, _ in ordered),
        f"总里程约 {distance_km:.1f} 公里，在途约 {duration_min:.0f} 分钟",
    ]
    if anchor is not None:
        lines.append(f"（已按出发点「{start.strip()}」起算顺序）")
    if unresolved:
        lines.append(
            f"⚠️ 未纳入排序的地点（解析失败）：{'、'.join(unresolved)}。"
            "这类失败多为重名，可把城市名带上重试（如「重庆长江索道」）"
        )
    if duration_min > _DAY_TRAVEL_WARN_MIN:
        lines.append(
            f"⚠️ 在途时间偏长（超过 {_DAY_TRAVEL_WARN_MIN:.0f} 分钟），建议减少一个地点或拆成两天"
        )

    return truncate_tool_output("\n".join(lines))


# ---------- 辅助 ----------


def _parse_location(value: str) -> tuple[float, float] | None:
    """把高德的 "lng,lat" 转成浮点坐标。"""
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点直线距离（公里），用于排序而不用来做结论。"""
    lng1, lat1 = a
    lng2, lat2 = b
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _nearest_neighbor_order(
    points: list[tuple[str, tuple[float, float]]],
    anchor: tuple[float, float] | None,
) -> list[tuple[str, tuple[float, float]]]:
    """贪心最近邻排序。起点优先用 anchor，没有则用列表第一个地点。"""
    remaining = list(points)
    ordered: list[tuple[str, tuple[float, float]]] = []
    current = anchor if anchor is not None else points[0][1]

    while remaining:
        nearest = min(remaining, key=lambda item: _haversine_km(current, item[1]))
        ordered.append(nearest)
        remaining.remove(nearest)
        current = nearest[1]

    return ordered
