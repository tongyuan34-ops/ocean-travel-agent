"""行程地图：地理编码 + 高德静态图（阶段 1）。

只做一件事：把一串地名变成一张带编号标记和连线的静态地图，落盘缓存后返回路径。
PDF 渲染不在这里，避免地图失败拖垮后续导出。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx

from app.agent.tools.info import _amap_key, _geocode
from app.agent.tools.plan import _parse_location
from app.core.config import PROJECT_ROOT

MAP_WIDTH = 1024
MAP_HEIGHT = 640
# 高德静态图 markers / labels 各最多 10 个；留一个给起点
MAX_STOPS = 9
CACHE_DIR = PROJECT_ROOT / "data" / "trips" / "maps"


class TripMapError(Exception):
    """地图无法生成：由路由层转成统一错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _label_text(prefix: str, name: str) -> str:
    """标签内容不能带空格，过长会挤占地图；最多保留 10 个字。"""
    return (prefix + re.sub(r"\s+", "", name))[:10]


def build_static_url(
    points: list[tuple[str, tuple[float, float]]],
    origin: tuple[str, tuple[float, float]] | None = None,
) -> str:
    """拼接高德静态图 URL：起点 + 途经点，带编号文字标签与连线。

    不传 location / zoom，让高德按所有覆盖物自动取景——
    实测手动指定中心与级别时，多标记只渲染第一个。
    """
    # 起点用「起」，途经点从 1 开始编号；没有起点时第一个点就是 1
    entries: list[tuple[str, str, tuple[float, float]]] = []
    if origin:
        entries.append(("起", origin[0], origin[1]))
    for stop_no, (name, coord) in enumerate(points, start=1):
        entries.append((str(stop_no), name, coord))
    coords = [coord for _, _, coord in entries]
    markers = "|".join(
        f"mid,0x{('C4553F' if prefix == '起' else '16263F')},{prefix}:{lng},{lat}"
        for prefix, _, (lng, lat) in entries
    )
    labels = "|".join(
        f"{_label_text(prefix, name)},0,1,12,0xFFFFFF,0x16263F:{lng},{lat}"
        for prefix, name, (lng, lat) in entries
    )

    query = urlencode(
        {
            "size": f"{MAP_WIDTH}*{MAP_HEIGHT}",
            "scale": "1",
            "markers": markers,
            "labels": labels,
            "key": _amap_key(),
        },
        safe="|,;:*",
    )
    path = ""
    if len(coords) >= 2:
        line = ";".join(f"{lng},{lat}" for lng, lat in coords)
        path = "&paths=" + quote(f"4,0x16263F,0.9,,:{line}", safe=",;:*")
    return f"https://restapi.amap.com/v3/staticmap?{query}{path}"


async def _locate(name: str, city: str) -> tuple[str, tuple[float, float]] | None:
    located = await _geocode(name, city or None)
    if located is None:
        return None
    coord = _parse_location(located[0])
    if coord is None:
        return None
    return name, coord


async def build_map(
    *,
    stops: list[str],
    city: str = "",
    origin: str = "",
) -> Path:
    """解析地名并返回缓存好的 PNG 路径。

    允许部分地名解析失败：只要还剩至少一个点就出图，
    解析失败的地点直接跳过，不影响其余点位与连线。
    """
    if not _amap_key():
        raise TripMapError("TOOL_NOT_ENABLED", "未配置 MAP_API_KEY，无法生成地图")

    names = [n.strip() for n in stops if n and n.strip()][:MAX_STOPS]
    if not names:
        raise TripMapError("INVALID_REQUEST", "至少需要一个地点")

    origin_point = None
    if origin.strip():
        origin_point = await _locate(origin.strip(), city)

    points: list[tuple[str, tuple[float, float]]] = []
    unresolved: list[str] = []
    for name in names:
        point = await _locate(name, city)
        if point is None:
            unresolved.append(name)
            continue
        points.append(point)

    if not points and origin_point is None:
        raise TripMapError("MAP_NOT_FOUND", f"所有地点都无法定位：{'、'.join(unresolved)}")

    url = build_static_url(points, origin_point)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"map-{digest}.png"
    if target.is_file():
        return target

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise TripMapError("MAP_FAILED", f"地图服务请求失败：{exc}") from None

    content_type = (resp.headers.get("content-type") or "").lower()
    if resp.status_code != 200 or not content_type.startswith("image/"):
        raise TripMapError("MAP_FAILED", f"地图服务返回异常（HTTP {resp.status_code}）")

    target.write_bytes(resp.content)
    return target
