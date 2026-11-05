"""行程路由：静态地图（阶段 1）与 PDF 导出（阶段 2）。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, Response

from app.core.errors import ApiError
from app.schemas.trips import TripPlan
from app.services.trip_export import PDF_DIR, build_trip_pdf, normalize
from app.services.trip_ics import render_ics
from app.services.trip_share import save_share
from app.services.trips import CACHE_DIR as MAP_DIR
from app.services.trips import TripMapError, build_map, maps_for_plan

router = APIRouter(prefix="/trips", tags=["trips"])

# 只放行自己生成的文件名：trip-<16 位 hex>.pdf / map-<16 位 hex>.png。
# 用白名单而不是「过滤掉 ..」——后者永远漏得掉一种编码方式。
_FILE_NAME = re.compile(r"^(?:trip|map)-[0-9a-f]{16}\.(?:pdf|png)$")


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


@router.get("/files/{name}")
async def trip_file(name: str) -> FileResponse:
    """把生成好的 PDF / 路线图发给前端。

    导出接口是「点一下就下载」，而助手侧的工具只能给一个链接——窗口里的
    浏览器要能把那份文件取回来，就得有个按名字取的地方。

    只认自己生成的文件名（``_FILE_NAME`` 白名单），不接受路径分隔符，
    所以拼不出 ``../`` 之类的越界读取。
    """
    if not _FILE_NAME.match(name):
        raise ApiError("INVALID_REQUEST", "文件名不合法")
    is_pdf = name.endswith(".pdf")
    path = (PDF_DIR if is_pdf else MAP_DIR) / name
    if not path.is_file():
        raise ApiError("NOT_FOUND", "文件不存在或已被清理，重新生成一次即可")
    return FileResponse(
        path,
        media_type="application/pdf" if is_pdf else "image/png",
        filename=name,
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _safe_title(title: str, suffix: str) -> str:
    clean = re.sub(r'[\\/:*?"<>|]+', "_", title).strip("_ ")
    return f"{clean or '行程'}{suffix}"


@router.post("/export")
async def export_trip(payload: TripPlan) -> FileResponse:
    """把行程渲染成 A4 PDF；地图失败时仍然出文档。"""
    try:
        out_path = await build_trip_pdf(payload)
    except RuntimeError as exc:
        raise ApiError("PDF_FAILED", str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - 渲染失败如实返回
        raise ApiError("PDF_FAILED", f"PDF 生成失败：{exc}") from None

    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=_safe_title(payload.title, ".pdf"),
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
    plan = normalize(payload)
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
    plan = normalize(payload)
    if not any(day.stops for day in plan.days):
        raise ApiError("INVALID_REQUEST", "没有可分享的地点")
    overview, day_maps = await maps_for_plan(plan)
    token, expires_at = save_share(plan, overview, day_maps)
    return {"path": f"/share/{token}", "expires_at": expires_at}
