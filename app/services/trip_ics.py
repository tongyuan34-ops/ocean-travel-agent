"""行程导出为 iCalendar（.ics）：每个地点一个事件。

时间规则：
- 优先使用每个 stop 自带的 HH:MM；
- 没有时间时，从 09:00 起按 1.5 小时递推，最晚不超过 20:00；
- 日期优先用 day.date，否则从 start_date 起逐天递增。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

from app.schemas.trips import TripPlan

_DEFAULT_SLOTS = [time(9, 0), time(11, 30), time(14, 0), time(16, 30), time(18, 30)]


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _parse_time(value: str, index: int) -> time:
    raw = (value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%H点%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    if index < len(_DEFAULT_SLOTS):
        return _DEFAULT_SLOTS[index]
    # 超出的地点：最后一个默认时段之后每小时一个，封顶 20:00
    hours = min(20, 19 + (index - len(_DEFAULT_SLOTS)) + 1)
    return time(hours, 0)


def _parse_date(value: str) -> date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def render_ics(plan: TripPlan, fallback_start: date) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ocean Travel Assistant//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    for day_index, day in enumerate(plan.days):
        day_date = _parse_date(day.date) or fallback_start + timedelta(days=day_index)
        for stop_index, stop in enumerate(day.stops):
            start_time = _parse_time(stop.time, stop_index)
            start_dt = datetime.combine(day_date, start_time)
            end_dt = start_dt + timedelta(minutes=90)
            summary = stop.name
            description = stop.note or ""
            location = "，".join(x for x in [stop.name, plan.city] if x)

            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:{uuid4()}@ocean-travel",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}",
                    f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%S')}",
                    f"SUMMARY:{_escape(summary)}",
                    f"LOCATION:{_escape(location)}",
                    f"DESCRIPTION:{_escape(description)}",
                    "END:VEVENT",
                ]
            )

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
