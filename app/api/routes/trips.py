"""行程路由：静态地图（阶段 1）与 PDF 导出（阶段 2）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response

from app.core.config import PROJECT_ROOT
from app.core.errors import ApiError
from app.schemas.trips import TripPlan
from app.services.trip_ics import render_ics
from app.services.trip_pdf import render_pdf
from app.services.trip_share import save_share
from app.services.trips import TripMapError, build_map

router = APIRouter(prefix="/trips", tags=["trips"])

PDF_DIR = PROJECT_ROOT / "data" / "trips" / "pdf"
MAX_DAYS = 15
MAX_STOPS_PER_DAY = 12


@router.get("/map")
async def trip_map(
    stops: list[str] = Query(default=[], description="按顺序排列的地点名称，可重复传参"),
    city: str = "",
    origin: str = "",
) -> FileResponse:
    """按地点顺序生成静态地图；服务端缓存，同一条线路只请求高德一次。"""
    try:
        path = await build_map(stops=stops, city=city, origin=origin)
    except TripMapError as exc:
        raise ApiError(exc.code, exc.message) from None
    return FileResponse(
        path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _normalize(payload: TripPlan) -> TripPlan:
    payload.title = (payload.title or "").strip()[:80] or "我的行程"
    payload.days = payload.days[:MAX_DAYS]
    for day in payload.days:
        day.stops = day.stops[:MAX_STOPS_PER_DAY]
    return payload


async def _maps_for_plan(
    plan: TripPlan,
) -> tuple[Path | None, list[Path | None]]:
    """总览图 + 每天一张图；单张失败不影响其它。"""
    overview: Path | None = None
    stops = [stop.name for day in plan.days for stop in day.stops]
    if stops:
        try:
            overview = await build_map(stops=stops, city=plan.city, origin=plan.origin)
        except TripMapError:
            overview = None

    day_maps: list[Path | None] = []
    for index, day in enumerate(plan.days):
        names = [stop.name for stop in day.stops]
        if not names:
            day_maps.append(None)
            continue
        try:
            day_maps.append(
                await build_map(
                    stops=names,
                    city=plan.city,
                    origin=plan.origin if index == 0 else "",
                )
            )
        except TripMapError:
            day_maps.append(None)
    return overview, day_maps


def _safe_title(title: str, suffix: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|]+', "_", title).strip("_ ")
    return f"{clean or '行程'}{suffix}"


@router.post("/export")
async def export_trip(payload: TripPlan) -> FileResponse:
    """把行程渲染成 A4 PDF；地图失败时仍然出文档。"""
    plan = _normalize(payload)
    overview, day_maps = await _maps_for_plan(plan)

    key = hashlib.sha1(
        (
            json.dumps(plan.model_dump(), ensure_ascii=False, sort_keys=True)
            + (overview.name if overview else "")
            + "".join(path.name if path else "-" for path in day_maps)
        ).encode("utf-8")
    ).hexdigest()[:16]
    out_path = PDF_DIR / f"trip-{key}.pdf"

    if not out_path.is_file():
        try:
            await asyncio.to_thread(render_pdf, plan, overview, day_maps, out_path)
        except RuntimeError as exc:
            raise ApiError("PDF_FAILED", str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - 渲染失败如实返回
            raise ApiError("PDF_FAILED", f"PDF 生成失败：{exc}") from None

    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=_safe_title(plan.title, ".pdf"),
    )


def _fallback_start(plan: TripPlan) -> date:
    raw = (plan.start_date or "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today() + timedelta(days=1)


@router.post("/export/ics")
async def export_ics(payload: TripPlan) -> Response:
    """每个地点一个日历事件；没有日期时从明天开始逐天排列。"""
    plan = _normalize(payload)
    if not any(day.stops for day in plan.days):
        raise ApiError("INVALID_REQUEST", "没有可导出的地点")
    text = render_ics(plan, _fallback_start(plan))
    filename = quote(_safe_title(plan.title, ".ics"))
    return Response(
        content=text,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


@router.post("/share")
async def create_share(payload: TripPlan) -> dict:
    """生成只读分享页，返回相对路径；前端用当前域名拼完整链接。"""
    plan = _normalize(payload)
    if not any(day.stops for day in plan.days):
        raise ApiError("INVALID_REQUEST", "没有可分享的地点")
    overview, day_maps = await _maps_for_plan(plan)
    token, expires_at = save_share(plan, overview, day_maps)
    return {"path": f"/share/{token}", "expires_at": expires_at}
