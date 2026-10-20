"""info 组工具：天气、路线、POI。

数据来源为高德开放平台 Web 服务 API。
未配置 Key 时返回 TOOL_NOT_ENABLED 文本，绝不编造数据（SPEC 底线一）。
"""

from __future__ import annotations

import httpx
from langchain_core.tools import tool

from app.core.config import Settings, load_settings
from app.core.context import truncate_tool_output

_AMAP = "https://restapi.amap.com"
_TIMEOUT = 15.0
# 超过这个距离就认为地址解析出错，拒绝把结果当作证据
_MAX_PLAUSIBLE_KM = 500.0

_settings_cache: Settings | None = None


def _s() -> Settings:
    """懒加载配置，避免每个工具都读一遍 .env。"""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache


def _amap_key() -> str:
    """高德的天气、路线、POI 共用同一个 Web 服务 Key。"""
    settings = _s()
    return settings.map_api_key or settings.weather_api_key


def _not_enabled(tool_name: str) -> str:
    return (
        f"TOOL_NOT_ENABLED：{tool_name} 未启用，"
        "请在 .env 中配置 MAP_API_KEY（高德开放平台 Web 服务 Key）"
    )


def _name_matches(query: str, resolved: str) -> bool:
    """校验地名解析结果是否与输入相符。

    用「输入前两个字是否出现在解析结果里」作为判据，而不是「任意一个字出现」——
    否则「市」「城」这类高频字会让垃圾输入（如「不存在的城市xyz」）
    蒙混过关，进而算出一条起点完全错误的路线。

    宁可误判为解析失败（工具会如实报错、请用户换更具体的地名），
    也不能放过错误解析——那会输出看似合理实则错误的结论。
    """
    cleaned = query.strip()
    if len(cleaned) < 2:
        return True
    return cleaned[:2] in resolved


async def _amap_get(path: str, params: dict[str, str]) -> dict:
    """调用高德接口并校验其业务状态码。"""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{_AMAP}{path}", params=params)
    try:
        data = resp.json()
    except ValueError:
        return {"status": "0", "info": f"返回非 JSON（HTTP {resp.status_code}）"}
    if not isinstance(data, dict):
        return {"status": "0", "info": "响应结构异常"}
    return data


async def _geocode(address: str, city: str | None = None) -> tuple[str, str] | None:
    """把地名转成 "lng,lat"。

    传入 city 可显著提高重名/模糊地名的解析准确度——
    否则「三亚市区」这类输入可能被匹配到全国任意同名地点。
    """
    params = {"key": _amap_key(), "address": address}
    if city:
        params["city"] = city
    data = await _amap_get("/v3/geocode/geo", params)
    if data.get("status") != "1":
        return None
    geocodes = data.get("geocodes") or []
    if not geocodes:
        return None

    item = geocodes[0]
    resolved = f"{item.get('formatted_address') or ''}{item.get('city') or ''}"
    if not _name_matches(address, resolved):
        # 高德会对无法识别的输入做模糊匹配，返回一个毫不相干的地点。
        # 必须在这里拦住，否则会算出比如「北京 → 三亚 2898 公里」这种
        # 看起来合理、实际起点完全错误的路线。
        return None

    return item.get("location"), item.get("city") or address


@tool
async def get_weather(city: str) -> str:
    """查询指定城市的天气。city 传城市名，如「三亚」「杭州」。"""
    if not _amap_key():
        return _not_enabled("get_weather")

    data = await _amap_get(
        "/v3/weather/weatherInfo",
        {"key": _amap_key(), "city": city, "extensions": "all"},
    )
    if data.get("status") != "1":
        return f"天气查询失败：{data.get('info') or '未知原因'}"

    forecasts = data.get("forecasts") or []
    if not forecasts:
        return f"暂无 {city} 的天气数据。"

    casts = forecasts[0].get("casts") or []
    lines = [f"{city} 天气预报（数据来源：高德开放平台）："]
    for cast in casts:
        lines.append(
            f"- {cast.get('date')} 星期{cast.get('week')}："
            f"白天{cast.get('dayweather')} {cast.get('daytemp')}℃，"
            f"夜间{cast.get('nightweather')} {cast.get('nighttemp')}℃，"
            f"{cast.get('daywind')}风{cast.get('daypower')}级"
        )
    return truncate_tool_output("\n".join(lines))


@tool
async def search_poi(city: str, keyword: str) -> str:
    """按关键词检索城市内的地点（景点、酒店、商圈等）。

    city 传城市名；keyword 传检索词，如「亚龙湾」「五星级酒店」。
    """
    if not _amap_key():
        return _not_enabled("search_poi")

    data = await _amap_get(
        "/v3/place/text",
        {"key": _amap_key(), "keywords": keyword, "city": city, "offset": "10"},
    )
    if data.get("status") != "1":
        return f"POI 检索失败：{data.get('info') or '未知原因'}"

    pois = data.get("pois") or []
    if not pois:
        return f"未找到 {city} 与「{keyword}」相关的地点。"

    lines = [f"{city}「{keyword}」检索结果（数据来源：高德开放平台）："]
    for poi in pois[:10]:
        lines.append(
            f"- {poi.get('name')}｜{poi.get('type') or '未分类'}｜"
            f"{poi.get('address') or '地址未提供'}｜坐标 {poi.get('location')}"
        )
    return truncate_tool_output("\n".join(lines))


@tool
async def plan_route(
    origin: str, destination: str, mode: str = "driving", city: str = ""
) -> str:
    """规划两地之间的路线。

    origin / destination 传地名，如「亚龙湾国家旅游度假区」「蜈支洲岛码头」；
    mode 传 driving（驾车）、walking（步行）或 bicycling（骑行）；
    city 建议传所在城市（如「三亚」），可显著提高地名解析准确度。
    """
    if not _amap_key():
        return _not_enabled("plan_route")

    if mode not in {"driving", "walking", "bicycling"}:
        mode = "driving"

    hint = city.strip() or None
    start = await _geocode(origin, hint)
    end = await _geocode(destination, hint)
    if start is None or end is None:
        return (
            f"无法解析起点或终点位置：{origin} → {destination}。"
            "请改用更具体的地名（例如景点全称或加上城市名）重试。"
        )

    data = await _amap_get(
        f"/v3/direction/{mode}",
        {"key": _amap_key(), "origin": start[0], "destination": end[0]},
    )
    if data.get("status") != "1":
        return f"路线规划失败：{data.get('info') or '未知原因'}"

    paths = (data.get("route") or {}).get("paths") or []
    if not paths:
        return f"未能为 {origin} → {destination} 找到可用路线。"

    path = paths[0]
    distance_km = int(path.get("distance") or 0) / 1000
    duration_min = int(path.get("duration") or 0) / 60

    # 合理性校验：城市内出行不可能上千公里，出现即说明地址解析错了，
    # 这种脏数据绝不能当成"证据"喂给模型。
    if distance_km > _MAX_PLAUSIBLE_KM:
        return (
            f"路线数据异常：{origin} → {destination} 返回距离 {distance_km:.0f} 公里，"
            f"明显不符合常规出行距离，该结果不可采用。"
            "通常是因为地名过于模糊被解析到了错误位置，"
            "请改用更具体的地名（例如景点全称，或补充 city 参数）重试。"
        )

    lines = [
        f"{origin} → {destination}（{mode}，数据来源：高德开放平台）",
        f"总距离约 {distance_km:.1f} 公里，预计耗时约 {duration_min:.0f} 分钟",
        "主要路段：",
    ]
    for step in (path.get("steps") or [])[:6]:
        lines.append(f"- {step.get('instruction')}")
    return truncate_tool_output("\n".join(lines))
