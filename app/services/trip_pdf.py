"""行程 PDF 渲染：ReportLab + 系统中文字体，A4 排版。

为什么不用 WeasyPrint：Windows 上依赖 GTK，部署成本高；
ReportLab 是纯 Python wheel，配一个 TTF 中文字体即可稳定输出。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.schemas.trips import TripPlan

FONT_NAME = "CJK"

# 中文字体按平台找：桌面版在 Windows 上跑，CI 和 Docker 在 Linux 上跑，
# 写死 C:/Windows/Fonts 的后果是「Windows 上一切正常，Linux 上导出 PDF 直接抛异常」——
# 测试也会跟着红，而且报错信息（找不到字体）跟真正的病因（写死了路径）看起来没关系。
FONT_CANDIDATES = (
    # Linux：Noto CJK 是各发行版最通用的一套，Docker 里 apt 装 fonts-noto-cjk 即可
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    # Windows
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
)

_MODE_TEXT = {"driving": "驾车", "walking": "步行", "bicycling": "骑行"}

TITLE = ParagraphStyle("title", fontName=FONT_NAME, fontSize=22, leading=30)
SUBTITLE = ParagraphStyle(
    "sub",
    fontName=FONT_NAME,
    fontSize=9.5,
    leading=15,
    textColor=colors.HexColor("#6B7A8C"),
)
DAY = ParagraphStyle(
    "day", fontName=FONT_NAME, fontSize=14, leading=22, spaceBefore=12, spaceAfter=6
)
CELL = ParagraphStyle("cell", fontName=FONT_NAME, fontSize=9, leading=14, wordWrap="CJK")
CELL_HEAD = ParagraphStyle(
    "head",
    fontName=FONT_NAME,
    fontSize=9,
    leading=14,
    textColor=colors.HexColor("#16263F"),
)
NOTE = ParagraphStyle(
    "note",
    fontName=FONT_NAME,
    fontSize=8,
    leading=13,
    textColor=colors.HexColor("#7A8798"),
)


def find_font() -> Path | None:
    """按 PDF_FONT_PATH → 各平台常见路径的顺序找一个可用的中文字体。"""
    explicit = os.getenv("PDF_FONT_PATH")
    if explicit:
        wanted = Path(explicit)
        # 显式指定的路径不给兜底：用户写了却找不到，多半是拼错了，静默换字体会更难查
        return wanted if wanted.is_file() else None
    for candidate in FONT_CANDIDATES:
        found = Path(candidate)
        if found.is_file():
            return found
    return None


def _register_font() -> str:
    path = find_font()
    if path is None:
        tried = "、".join(FONT_CANDIDATES)
        raise RuntimeError(
            f"找不到中文字体。请设置 PDF_FONT_PATH 指向一个中文字体文件，"
            f"或安装其中任意一套：{tried}"
        )
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(path)))
    return FONT_NAME


def _footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(FONT_NAME, 8)
    canvas.setFillColor(colors.HexColor("#8A97A8"))
    canvas.drawString(18 * mm, 10 * mm, "Ocean 旅行助手")
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"第 {doc.page} 页")
    canvas.restoreState()


def _day_table(day) -> Table:
    rows = [
        [
            Paragraph("时间", CELL_HEAD),
            Paragraph("地点", CELL_HEAD),
            Paragraph("备注", CELL_HEAD),
        ]
    ]
    for stop in day.stops:
        rows.append(
            [
                Paragraph(escape(stop.time or "—"), CELL),
                Paragraph(escape(stop.name), CELL),
                Paragraph(escape(stop.note or "—"), CELL),
            ]
        )
    table = Table(rows, colWidths=[24 * mm, 94 * mm, 56 * mm], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF8")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9D4E4")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def render_pdf(
    plan: TripPlan,
    overview_path: Path | None,
    day_maps: list[Path | None],
    out_path: Path,
) -> Path:
    """同步渲染；调用方应放进线程池，避免阻塞事件循环。"""
    _register_font()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=plan.title,
        author="Ocean 旅行助手",
    )

    story: list = [Paragraph(escape(plan.title), TITLE)]
    meta = []
    if plan.city:
        meta.append(plan.city)
    meta.append(_MODE_TEXT.get(plan.mode, plan.mode or "驾车"))
    meta.append(f"{len(plan.days)} 天")
    meta.append(time.strftime("%Y-%m-%d"))
    story.append(Paragraph(" · ".join(meta), SUBTITLE))
    story.append(Spacer(1, 8))

    if overview_path is not None and overview_path.is_file():
        width = 174 * mm
        story.append(Image(str(overview_path), width=width, height=width * 640 / 1024))

    if plan.origin:
        story.append(Spacer(1, 6))
        story.append(Paragraph(f"出发地：{escape(plan.origin)}", CELL))

    if not plan.days:
        story.append(Spacer(1, 10))
        story.append(Paragraph("（未提供每日安排）", CELL))

    for index, day in enumerate(plan.days, start=1):
        heading = day.title or f"第 {index} 天"
        if day.date:
            heading = f"{heading}（{day.date}）"
        story.append(Paragraph(escape(heading), DAY))
        day_map = day_maps[index - 1] if index - 1 < len(day_maps) else None
        if day_map is not None and day_map.is_file():
            width = 150 * mm
            story.append(Image(str(day_map), width=width, height=width * 640 / 1024))
            story.append(Spacer(1, 4))
        if day.stops:
            story.append(_day_table(day))
        else:
            story.append(Paragraph("（未安排地点）", CELL))

    story.append(Spacer(1, 10))
    story.append(
        Paragraph(
            "说明：地图与地点位置来自高德开放平台；行程顺序按规划结果排列，"
            "门票、票价与营业时间请以官方渠道为准。",
            NOTE,
        )
    )

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
