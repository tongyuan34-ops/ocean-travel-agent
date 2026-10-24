"""交通推荐：给出真实驾车里程与耗时，并列出查班次票价的官方入口。

能核实的只有「驾车」这一种方式——里程与耗时由高德路线接口算出。
高铁/航班/长途大巴的**班次时刻与票价没有合规数据来源**，
因此只给方式适用性的经验判断（明确标注为经验参考）与官方查询入口，
不编造任何班次号和价格。
"""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.tools.info import _amap_get, _amap_key, _geocode, _not_enabled
from app.agent.tools.plan import _haversine_km, _parse_location
from app.core.context import truncate_tool_output

_MAX_PLAUSIBLE_KM = 8000.0
_LONG_DRIVE_HOURS = 8.0

# 官方查询入口（稳定地址）
_QUERY_ENTRY = {
    "铁路（12306 官方）": "https://www.12306.cn/",
    "机票（携程）": "https://flights.ctrip.com/",
}

# 距离 → 方式适用性判断。这是出行常识性经验，不是票价或班次数据。
_DISTANCE_ADVICE = [
    (300, "短途：驾车 3 小时内可达；高铁通常更快更省心；飞机加上往返机场的时间一般不占优"),
    (800, "中途：高铁与飞机各有优势（高铁 2–4 小时区间较舒适）；驾车约 4–8 小时，视路况而定"),
    (1500, "长途：飞机优势明显；高铁若总时长在 6 小时内仍可考虑"),
    (float("inf"), "超长途：建议飞机；驾车跨越多省不建议，耗时与疲劳都不划算"),
]


class RecommendTransportInput(BaseModel):
    origin: str = Field(description="出发地，如 北京 / 北京南站")
    destination: str = Field(description="目的地，如 三亚 / 三亚凤凰国际机场")
    origin_city: str = Field(
        default="", description="出发地所在城市，如 北京；强烈建议填写，避免同名地点解析错位"
    )
    destination_city: str = Field(default="", description="目的地所在城市，如 三亚")
    date: str = Field(default="", description="出发日期 YYYY-MM-DD；留空表示未定")
    budget: float = Field(default=0, description="交通预算（人民币元）；0 表示未设定")
    prefer: str = Field(default="", description="偏好：最快 / 最省 / 舒适；留空表示无特别偏好")


@tool(args_schema=RecommendTransportInput)
async def recommend_transport(
    origin: str,
    destination: str,
    origin_city: str = "",
    destination_city: str = "",
    date: str = "",
    budget: float = 0,
    prefer: str = "",
) -> str:
    """对比两地之间的交通方式。

    能给出**真实驾车里程与耗时**（高德路线接口计算），
    并按距离给出各方式的适用性经验判断。**不提供班次时刻与票价**，
    只给出查询班次的官方入口。
    """
    if not _amap_key():
        return _not_enabled("recommend_transport")

    origin = origin.strip()
    destination = destination.strip()
    if not origin or not destination:
        return "请先确认出发地和目的地。"

    start = await _geocode(origin, origin_city.strip() or None)
    end = await _geocode(destination, destination_city.strip() or None)
    if start is None or end is None:
        return (
            f"无法解析出发地或目的地：{origin} → {destination}。"
            "请改用更具体的地名（例如站点全称），或补充 origin_city / destination_city。"
        )

    start_coord = _parse_location(start[0])
    end_coord = _parse_location(end[0])
    if start_coord is None or end_coord is None:
        return "坐标解析失败，请改用更具体的地名重试。"

    straight_km = _haversine_km(start_coord, end_coord)

    conditions = [f"{origin} → {destination}"]
    if date.strip():
        conditions.append(date.strip())
    if budget > 0:
        conditions.append(f"预算 {budget:,.0f} 元")
    if prefer.strip():
        conditions.append(f"偏好：{prefer.strip()}")

    lines = [f"两地交通对比（{' · '.join(conditions)}）", ""]
    lines.append(f"直线距离约 {straight_km:,.0f} 公里")

    # 唯一可核实的方式：驾车（真实里程与耗时）
    drive = await _amap_get(
        "/v3/direction/driving",
        {
            "key": _amap_key(),
            "origin": f"{start_coord[0]},{start_coord[1]}",
            "destination": f"{end_coord[0]},{end_coord[1]}",
        },
    )
    if drive.get("status") == "1":
        paths = (drive.get("route") or {}).get("paths") or []
        if paths:
            distance_km = int(paths[0].get("distance") or 0) / 1000
            duration_h = int(paths[0].get("duration") or 0) / 3600
            if distance_km <= _MAX_PLAUSIBLE_KM:
                lines.append(
                    f"驾车（高德路线计算，含高速）：约 {distance_km:,.0f} 公里，"
                    f"纯驾驶约 {duration_h:.1f} 小时"
                )
                if duration_h > _LONG_DRIVE_HOURS:
                    lines.append(
                        f"⚠️ 纯驾驶已超过 {_LONG_DRIVE_HOURS:.0f} 小时，"
                        "单人长途驾驶疲劳风险高，不建议"
                    )
            else:
                lines.append("驾车：未见合理陆路路线（可能跨海或缺陆路连接）")
    else:
        lines.append(f"驾车：路线计算失败（{drive.get('info') or '未知原因'}）")

    lines.append("")
    lines.append("【方式适用性】经验参考，不含班次与票价")
    lines.append(f"- {_advice(straight_km)}")
    lines.append("- 若同行人多、行李多、目的地分散，自驾的机动性会明显加分")
    lines.append("- 跨海或缺少陆路连接的线路，只能选飞机或轮渡")

    lines.append("")
    lines.append("【查班次与票价的官方入口】")
    for name, url in _QUERY_ENTRY.items():
        lines.append(f"- {name}：{url}")
    lines.append("")
    lines.append(
        "⚠️ 本工具不提供任何班次时刻与票价——这类数据没有合规来源，请以上述官方渠道的实时查询为准。"
    )

    return truncate_tool_output("\n".join(lines))


def _advice(straight_km: float) -> str:
    for upper, text in _DISTANCE_ADVICE:
        if straight_km <= upper:
            return text
    return "超长途：建议飞机"
