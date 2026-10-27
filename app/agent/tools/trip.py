"""行程规划：把多个目的地按顺序串成一条线路。

能核实的是「驾车」：每一段的里程与耗时都由高德路线接口算出，逐段累加，
不是估算。并为每段生成导航入口。

关于和另外两个「路线」工具的分工（这条是踩出来的）：
`plan_route` 只算两点之间的一段路，`optimize_day_route` 只给已定好的一天排顺序，
本工具负责跨天、多目的地的整体串联。三者描述里都写死了「什么时候不要用我」——
早先只写「我做什么」，40 条评测里 `trip-chongqing-2d` 三轮都选了
`optimize_day_route`（因为「顺路」这个词只出现在它的描述里），
`plan-then-optimize` 也漏掉了「先规划三天行程」这一步。

关于 origin / stops 可以为空：
「重庆两天怎么玩」这类请求里用户一个地点都没给，早先的工具直接回
「请提供出发地和至少一个目的地」，等于把一个能答的问题推回给用户。
现在只给 city + days 就会按城市热度自动选点，并在结果里明确标注
「这些是候选，可按偏好替换」——自动选点是为了让工具可用，不是为了替用户做决定。

关于导航链接的取舍：
- 用高德手机版的 App scheme（`lat=` / `lon=` 是具名参数），**不存在坐标顺序歧义**，
  在手机上点击可直接进入导航；
- `uri.amap.com/navigation` 网页版的坐标顺序与 marker 接口相反，
  尚未实测确认，拼错会把用户导到完全错误的位置，因此本工具不输出该链接。
"""

from __future__ import annotations

from urllib.parse import quote

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.tools.info import _amap_get, _amap_key, _geocode, _not_enabled
from app.agent.tools.plan import _haversine_km, _parse_location
from app.core.context import truncate_tool_output

_MAX_STOPS = 8
_MAX_PLAUSIBLE_KM = 8000.0
_LIST_LIMIT = 1600
# 单日在途超过这个小时数就提示节奏过赶
_DAY_TRAVEL_WARN_HOURS = 5.0
# 顺序优化能省下超过这个比例才提示
_ORDER_GAIN_THRESHOLD = 0.2


class PlanTripInput(BaseModel):
    origin: str = Field(
        default="", description="出发地，如 三亚凤凰国际机场；用户没说就留空，以第一个目的地为起点"
    )
    stops: list[str] = Field(
        default_factory=list,
        description=(
            "要串起来的目的地，大致顺序即可，如 [亚龙湾国家旅游度假区, 蜈支洲岛码头]；"
            "可以是用户点名的，也可以是 search_poi 查到的候选；留空则按 city + days 自动选点"
        ),
    )
    city: str = Field(
        default="", description="所在城市，如 三亚；强烈建议填写，否则地名可能解析错位"
    )
    days: int = Field(
        default=0, description="总天数；用于判断节奏，留空 stops 时也用它决定选几个点"
    )
    mode: str = Field(
        default="driving", description="driving（驾车）/ walking（步行）/ bicycling（骑行）"
    )


@tool(args_schema=PlanTripInput)
async def plan_trip(
    origin: str = "",
    stops: list[str] | None = None,
    city: str = "",
    days: int = 0,
    mode: str = "driving",
) -> str:
    """把**多天行程**串成一条可执行的线路（跨天、多个目的地）。

    **用户提到「N 天行程」「怎么玩」「帮我规划几天」「顺路安排」这类跨天、多目的地的请求时，
    默认用这个工具。** stops 可以是用户点名的地点，也可以是你先用 search_poi 找到的候选；
    两样都没有时只传 city + days，我会按城市热度自动挑几个景点再串起来。

    只要还有多天或多段要串，就不要拿 plan_route（只算两点之间的一段路）
    或 optimize_day_route（只给已定好的一天排顺序）来代替——那两个看不到全局。

    逐段给出**真实里程与耗时**（高德路线接口计算），汇总在途总量，
    并按天数判断节奏是否合理；同时为每个目的地生成手机端导航入口。
    不提供任何票价、班次与门票信息。
    """
    if not _amap_key():
        return _not_enabled("plan_trip")

    origin = origin.strip()
    names = [name.strip() for name in (stops or []) if name and name.strip()]
    hint = city.strip() or None

    auto_points: list[tuple[str, tuple[float, float]]] = []
    if not names:
        if not hint or days <= 0:
            return (
                "需要至少一个目的地；也可以只给 city 与 days（如 city=重庆, days=2），"
                "我来按城市热度挑几个景点再串。"
            )
        auto_points = await _auto_points(hint, days)
        if not auto_points:
            return (
                f"未能自动找到 {hint} 的候选景点。"
                "请先用 search_poi 查几个地点，再把名字传进 stops。"
            )
        names = [name for name, _ in auto_points]

    if len(names) > _MAX_STOPS:
        names = names[:_MAX_STOPS]
        truncated = True
    else:
        truncated = False

    # 用户没说从哪出发时，以第一个目的地为起点。
    # 拿城市中心当起点会凭空多出一段没有意义的里程，还会把「日均在途」算高。
    if not origin:
        if len(names) < 2:
            return "至少需要两个地点才能串成线路（一个起点加一个目的地）。"
        origin = names[0]
        names = names[1:]

    # 自动选点时高德已经把坐标给了，不必再逐个地理编码（省掉 N 次网络往返）
    known = dict(auto_points)

    # 起点也走同一张表：自动选点时 origin 就是第一个候选点，再查一次纯属浪费
    origin_coord = known.get(origin)
    if origin_coord is None:
        start = await _geocode(origin, hint)
        if start is None:
            return f"无法解析出发地「{origin}」，请改用更具体的地名后重试。"
        origin_coord = _parse_location(start[0])
        if origin_coord is None:
            return "出发地坐标解析失败，请改用更具体的地名重试。"

    points: list[tuple[str, tuple[float, float]]] = []
    unresolved: list[str] = []
    for name in names:
        if name in known:
            points.append((name, known[name]))
            continue
        located = await _geocode(name, hint)
        coord = _parse_location(located[0]) if located else None
        if coord is None:
            unresolved.append(name)
            continue
        points.append((name, coord))

    if not points:
        return (
            f"以下地点均无法解析：{'、'.join(unresolved)}。"
            "请改用更具体的地名（景点全称）或补充 city 参数后重试。"
        )

    if mode not in {"driving", "walking", "bicycling"}:
        mode = "driving"

    # 逐段计算真实里程与耗时
    legs: list[tuple[str, str, float, float]] = []
    current_name, current_coord = origin, origin_coord
    for name, coord in points:
        data = await _amap_get(
            f"/v3/direction/{mode}",
            {
                "key": _amap_key(),
                "origin": f"{current_coord[0]},{current_coord[1]}",
                "destination": f"{coord[0]},{coord[1]}",
            },
        )
        paths = (data.get("route") or {}).get("paths") if data.get("status") == "1" else None
        if not paths:
            legs.append((current_name, name, -1.0, -1.0))
        else:
            distance_km = int(paths[0].get("distance") or 0) / 1000
            duration_h = int(paths[0].get("duration") or 0) / 3600
            legs.append((current_name, name, distance_km, duration_h))
        current_name, current_coord = name, coord

    lines = [f"行程串联（{origin} 出发 · {len(points)} 个目的地 · {mode}）"]
    header = ["里程与耗时为实际路线计算，非估算"]
    if auto_points:
        header.append(
            "⚠️ 用户没有指定具体地点，下面是按城市热度自动挑的候选景点，"
            "可以按用户偏好替换后再调用一次"
        )
    if unresolved:
        header.append(f"⚠️ 未纳入线路的地点（解析失败）：{'、'.join(unresolved)}")
    if truncated:
        header.append(f"⚠️ 目的地超过 {_MAX_STOPS} 个，只规划了前 {_MAX_STOPS} 个")
    lines.extend(header)
    lines.append("")

    total_km = 0.0
    total_h = 0.0
    for index, (from_name, to_name, distance_km, duration_h) in enumerate(legs, start=1):
        lines.append(f"第 {index} 段　{from_name} → {to_name}")
        if distance_km < 0:
            lines.append("   路线计算失败，该段未计入合计")
            continue
        lines.append(f"   约 {distance_km:,.1f} 公里，纯行驶约 {duration_h:.1f} 小时")
        total_km += distance_km
        total_h += duration_h
        coord = points[index - 1][1]
        lines.append(f"   导航：{_nav_scheme(to_name, coord)}")

    lines.append("")
    if total_km > _MAX_PLAUSIBLE_KM:
        lines.append(f"⚠️ 合计里程 {total_km:,.0f} 公里异常偏大，可能有个别地名解析错位，请核对")
    else:
        lines.append(f"合计：约 {total_km:,.0f} 公里，纯行驶约 {total_h:.1f} 小时")

    if days > 0 and total_h > 0:
        per_day = total_h / days
        lines.append(f"按 {days} 天安排，日均在途约 {per_day:.1f} 小时")
        if per_day > _DAY_TRAVEL_WARN_HOURS:
            lines.append(
                f"⚠️ 日均在途超过 {_DAY_TRAVEL_WARN_HOURS:.0f} 小时，节奏偏赶，"
                "建议增加天数或减少目的地"
            )
        else:
            lines.append("节奏合理，留有余量游玩")

    # 顺序优化提示：只给真实算出来的差额，不猜
    gain = _order_gain(origin_coord, points)
    if gain is not None and gain >= _ORDER_GAIN_THRESHOLD:
        lines.append(
            f"提示：当前顺序不是里程最短的，按地理邻近重排可少走约 {gain * 100:.0f}% 的路程；"
            "若顺序有特殊安排可忽略（另可用 optimize_day_route 做单日排序）"
        )

    lines.append("")
    lines.append(
        "导航说明：上面的链接为高德手机版 App scheme，手机上点击可直接进入导航；"
        "iOS 把 androidamap:// 换成 iosamap:// 即可；电脑上请在高德网页版搜索该地点名称。"
    )
    lines.append("本工具不提供票价、班次与门票信息，相关费用请以官方渠道为准。")

    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)


# ---------- 辅助 ----------


async def _auto_points(city: str, days: int) -> list[tuple[str, tuple[float, float]]]:
    """用户没给具体地点时，按城市热度挑候选景点。

    每天两个点，最多 ``_MAX_STOPS`` 个——再多就不是「行程」而是清单了。

    用高德检索而不是内置景点清单：清单会过期，检索结果至少是当下真实存在、
    有坐标的 POI。检索词固定「景点」并按热度排序，是因为「热门景点」会把
    几十公里外的区县景点排进来（实测重庆会给出永川的茶山竹海），
    串成线路后里程和耗时全是噪音。

    返回坐标而不是名字：POI 检索已经带了 ``location``，
    再走一遍地理编码等于白花 N 次网络往返。
    """
    limit = min(days * 2, _MAX_STOPS)
    data = await _amap_get(
        "/v3/place/text",
        {
            "key": _amap_key(),
            "keywords": "景点",
            "city": city,
            "offset": str(limit),
            "sortrule": "weight",
        },
    )
    if data.get("status") != "1":
        return []

    picked: list[tuple[str, tuple[float, float]]] = []
    seen: set[str] = set()
    for poi in data.get("pois") or []:
        name = str(poi.get("name") or "").strip()
        coord = _parse_location(str(poi.get("location") or ""))
        if not name or coord is None or name in seen:
            continue
        seen.add(name)
        picked.append((name, coord))
        if len(picked) >= limit:
            break
    return picked


def _nav_scheme(name: str, coord: tuple[float, float]) -> str:
    """高德手机版导航 scheme。

    lat / lon 是具名参数，不存在坐标顺序歧义；这是刻意选择的结果——
    网页版 navigation 接口的顺序与 marker 相反且未实测确认。
    """
    lng, lat = coord
    return (
        "androidamap://navi?sourceApplication=travel-assistant"
        f"&poiname={quote(name)}&lat={lat}&lon={lng}&dev=0&style=2"
    )


def _order_gain(
    origin: tuple[float, float], points: list[tuple[str, tuple[float, float]]]
) -> float | None:
    """按地理邻近重排相对当前顺序能省下的比例。

    用直线距离做估算，只用于提示，不作为结论。
    """
    if len(points) < 3:
        return None

    def path_length(seq: list[tuple[str, tuple[float, float]]]) -> float:
        total = 0.0
        current = origin
        for _, coord in seq:
            total += _haversine_km(current, coord)
            current = coord
        return total

    current_length = path_length(points)
    if current_length <= 0:
        return None

    remaining = list(points)
    ordered: list[tuple[str, tuple[float, float]]] = []
    cursor = origin
    while remaining:
        nearest = min(remaining, key=lambda item: _haversine_km(cursor, item[1]))
        ordered.append(nearest)
        remaining.remove(nearest)
        cursor = nearest[1]

    best_length = path_length(ordered)
    # 顺序未变说明当前已是最短，不必提示
    if [name for name, _ in ordered] == [name for name, _ in points]:
        return None
    return max(0.0, (current_length - best_length) / current_length)
