"""把行程渲染成 A4 PDF 并落盘缓存。

**为什么单独一层**

PDF 有两个入口：``POST /trips/export``（前端按钮点一下就下载）和助手侧的
``export_trip_pdf`` 工具（用户说「帮我出一份」时调用）。两边都要「补地图 →
渲染 → 按内容哈希缓存」这一套。各写一份的结果是缓存键不一致：同一份行程
从按钮和从对话生成会出两份文件，还都留在磁盘上。

缓存键取「行程内容 + 用到的那几张地图文件名」，所以同一条行程重复生成
只渲染一次；换了出发日期或加了一个点，键就变了，不会拿到旧文档。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from app.core.config import PROJECT_ROOT
from app.schemas.trips import TripDay, TripPlan, TripStop
from app.services.trip_pdf import render_pdf
from app.services.trips import maps_for_plan

PDF_DIR = PROJECT_ROOT / "data" / "trips" / "pdf"
# 与路由层的上限一致：超过这个量级的行程，PDF 已经没人看得下去了
MAX_DAYS = 15
MAX_STOPS_PER_DAY = 12


def normalize(plan: TripPlan) -> TripPlan:
    """裁掉超限内容，并把空标题补成「我的行程」。"""
    plan.title = (plan.title or "").strip()[:80] or "我的行程"
    plan.days = plan.days[:MAX_DAYS]
    for day in plan.days:
        day.stops = day.stops[:MAX_STOPS_PER_DAY]
    return plan


def from_day_names(
    *,
    title: str = "",
    city: str = "",
    origin: str = "",
    mode: str = "driving",
    start_date: str = "",
    days: list[tuple[str, str, list[str]]],
) -> TripPlan:
    """从「(日期, 主题, 地点名列表)」拼出 TripPlan。

    助手侧工具拿到的是最朴素的形状——每天一串地名。要它填 time/note
    这些字段只会让模型多编造，不如留空，PDF 里就是干净的地点列表。
    """
    return TripPlan(
        title=title,
        city=city,
        origin=origin,
        mode=mode or "driving",
        start_date=start_date,
        days=[
            TripDay(
                date=day_date,
                title=day_title,
                stops=[TripStop(name=name) for name in names if name.strip()],
            )
            for day_date, day_title, names in days
        ],
    )


async def build_trip_pdf(plan: TripPlan) -> Path:
    """生成（或复用缓存的）PDF，返回文件路径。

    地图失败不影响出文档：``maps_for_plan`` 单张失败返回 None，
    PDF 那边会跳过那张图。行程单的价值在文字安排，不该被一张底图拖住。
    """
    plan = normalize(plan)
    overview, day_maps = await maps_for_plan(plan)

    key = hashlib.sha1(
        (
            json.dumps(plan.model_dump(), ensure_ascii=False, sort_keys=True)
            + (overview.name if overview else "")
            + "".join(path.name if path else "-" for path in day_maps)
        ).encode("utf-8")
    ).hexdigest()[:16]
    out_path = PDF_DIR / f"trip-{key}.pdf"

    if not out_path.is_file():
        await asyncio.to_thread(render_pdf, plan, overview, day_maps, out_path)
    return out_path
