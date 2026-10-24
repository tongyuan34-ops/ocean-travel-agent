"""住宿推荐：输出**具体酒店**清单与逐家的可点击链接。

思路（不接任何价格 API）：
1. 用高德 POI 检索（types=100000 住宿服务）拿到**真实存在的具体酒店**——
   名称、地址、坐标、电话、星级；
   高德的 type 字段自带星级（「五星级宾馆」「四星级宾馆」），可用于按预算筛选。
2. 每家酒店生成高德 URI API 的地图链接（官方能力，PC 与移动端均适用），
   点开即显示这家店的位置、电话与评价。
3. 另给一条按酒店名的平台搜索路径，用于查实时房价。

**不提供房价**：预订页链接需要各平台的内部 hotelId，无公开来源，
本工具不猜、不拼——只把用户准确送到能查到价格的地方。
"""

from __future__ import annotations

from urllib.parse import quote

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.tools.info import _amap_get, _amap_key, _not_enabled
from app.core.context import truncate_tool_output

_HOTEL_TYPE = "100000"  # 高德「住宿服务」大类
_MAX_HOTELS = 5
_LIST_LIMIT = 1850  # 酒店清单比普通工具输出长一些

# 预算（每晚元）→ 星级关键词；空字符串表示不限星级
_BUDGET_TO_STAR = [
    (300, ""),
    (700, "4"),
    (1500, "4"),
    (float("inf"), "5"),
]
_STAR_KEYWORD = {"5": "五星级酒店", "4": "四星级酒店", "3": "三星级酒店"}
_STAR_LABEL = {"5": "五星级", "4": "四星级", "3": "三星级"}

_PLATFORM_SEARCH = {
    "携程": "https://hotels.ctrip.com/",
    "飞猪": "https://www.fliggy.com/hotel/",
    "美团": "https://hotel.meituan.com/",
}


class RecommendStayInput(BaseModel):
    city: str = Field(description="目的地城市，如 三亚")
    area: str = Field(default="", description="区域偏好，如 亚龙湾 / 三亚湾；留空表示不限")
    checkin: str = Field(default="", description="入住日期 YYYY-MM-DD；留空表示未定")
    checkout: str = Field(default="", description="退房日期 YYYY-MM-DD；留空表示未定")
    adults: int = Field(default=2, description="成人数")
    rooms: int = Field(default=1, description="房间数")
    budget_per_night: float = Field(default=0, description="每晚预算（人民币元）；0 表示未设定")
    room_type: str = Field(default="", description="房型偏好，如 大床房 / 双床房 / 家庭房")
    star_rating: str = Field(default="", description="指定星级 5 / 4 / 3；留空则由预算推断")


@tool(args_schema=RecommendStayInput)
async def recommend_stay(
    city: str,
    area: str = "",
    checkin: str = "",
    checkout: str = "",
    adults: int = 2,
    rooms: int = 1,
    budget_per_night: float = 0,
    room_type: str = "",
    star_rating: str = "",
) -> str:
    """按预算与区域检索出**具体酒店**，给出逐家的地图链接与订房查询方式。

    酒店清单来自高德真实 POI 数据；不返回任何房价，价格以平台实时报价为准。
    """
    if not _amap_key():
        return _not_enabled("recommend_stay")

    city = city.strip()
    if not city:
        return "请先确认目的地城市。"
    area = area.strip()

    star = star_rating.strip() or _infer_star(budget_per_night)
    # 不用星级词做检索词：高德对「五星级酒店」这类词的召回不稳定，
    # 返回的 POI 里未必带星级标记。改为先取较大的候选池，再按 type 字段筛选。
    search_words = f"{area} 酒店".strip() if area else "酒店"

    data = await _amap_get(
        "/v3/place/text",
        {
            "key": _amap_key(),
            "keywords": search_words,
            "city": city,
            "types": _HOTEL_TYPE,
            "offset": "25",
            "extensions": "all",
        },
    )
    if data.get("status") != "1":
        return f"酒店检索失败：{data.get('info') or '未知原因'}"

    pois, fell_back = _pick_hotels(data.get("pois") or [], star)
    if not pois:
        return (
            f"未能在 {city}{('·' + area) if area else ''} 检索到符合条件的具体酒店。"
            "建议放宽区域或星级要求后重试。"
        )

    header = [f"酒店候选（{city}{('·' + area) if area else ''}）"]
    condition = []
    if budget_per_night > 0:
        condition.append(f"每晚预算 {budget_per_night:,.0f} 元")
    if star:
        condition.append(_STAR_LABEL.get(star, f"{star} 星级"))
    if room_type:
        condition.append(room_type)
    if checkin and checkout:
        condition.append(f"{checkin} 至 {checkout}")
    if condition:
        header.append("筛选条件：" + " · ".join(condition))
    if fell_back:
        header.append(
            f"⚠️ 该区域未检索到「{_STAR_LABEL.get(star, star)}」的酒店，"
            "以下已放宽为全部住宿类型，星级仅供参考，请自行核实"
        )
    header.append("（以下酒店均为真实 POI，地址与坐标已核实；本工具不提供房价）")
    header.append("")

    lines = header
    for index, poi in enumerate(pois, start=1):
        name = poi.get("name") or "未命名"
        star_text = _star_of(poi.get("type") or "")
        address = poi.get("address") or "地址未提供"
        tel = poi.get("tel")
        location = poi.get("location") or ""

        title = f"{index}. {name}"
        if star_text:
            title += f"（{star_text}）"
        lines.append(title)
        lines.append(f"   地址：{address}")
        if tel and isinstance(tel, str) and tel.strip():
            lines.append(f"   电话：{tel.strip()}")
        if location:
            lines.append(f"   地图：{_amap_marker_url(location, name)}")

    lines.append("")
    lines.append("【查实时房价】把上面的酒店名粘进下面任一平台的搜索框即可：")
    for name, url in _PLATFORM_SEARCH.items():
        lines.append(f"   {name}：{url}")
    if checkin and checkout:
        lines.append(
            f"   Booking.com（可直接按区域与日期搜）："
            f"{_booking_url((city + ' ' + area).strip(), checkin, checkout, adults, rooms)}"
        )
    lines.append("")
    lines.append("说明：预订单页链接需要各平台内部酒店 ID，无公开来源，故不提供；")
    lines.append("房价一律以平台实时报价为准，本工具不给出任何价格数字。")

    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)


# ---------- 辅助 ----------


def _infer_star(budget_per_night: float) -> str:
    if budget_per_night <= 0:
        return ""
    for upper, star in _BUDGET_TO_STAR:
        if budget_per_night <= upper:
            return star
    return "5"


def _star_of(poi_type: str) -> str:
    for star, label in _STAR_LABEL.items():
        if f"{star}星级宾馆" in poi_type:
            return label
    return ""


def _pick_hotels(pois: list[dict], star: str) -> tuple[list[dict], bool]:
    """按星级筛选。

    返回 (酒店列表, 是否放宽了筛选)。宽松时必须如实告知，
    否则会出现「标着四星级、实际返回青年旅舍」这种误导。
    """
    named = [poi for poi in pois if poi.get("name")]
    if not star:
        return named[:_MAX_HOTELS], False

    # 注意：高德 type 字段用的是中文数字，如「五星级宾馆」，
    # 不能用阿拉伯数字去拼，否则永远匹配不上。
    want = f"{_STAR_LABEL.get(star, star)}宾馆"
    matched = [poi for poi in named if want in (poi.get("type") or "")]
    if matched:
        return matched[:_MAX_HOTELS], False
    return named[:_MAX_HOTELS], True


def _amap_marker_url(location: str, name: str) -> str:
    """高德 URI API：PC 与移动端均可打开，直接显示该 POI 的位置。"""
    return (
        "https://uri.amap.com/marker?position="
        f"{location}&name={quote(name)}&coordinate=gaode&callnative=0"
    )


def _booking_url(dest: str, checkin: str, checkout: str, adults: int, rooms: int) -> str:
    parts = [f"ss={quote(dest)}", f"checkin={checkin}", f"checkout={checkout}"]
    parts.append(f"no_rooms={max(1, rooms)}")
    parts.append(f"group_adults={max(1, adults)}")
    return "https://www.booking.com/searchresults.zh-cn.html?" + "&".join(parts)
