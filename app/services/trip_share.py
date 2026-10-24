"""只读分享页：把行程渲染成自包含 HTML，按随机 token 存放。

设计取舍：
- 分享页**不依赖登录态、不引用会话**，地图直接内嵌 base64，避免对方还需要密码；
- token 用 urlsafe 随机串，默认 14 天过期；
- 只写 data/trips/shares/，不触碰用户目录。
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from pathlib import Path

from app.core.config import PROJECT_ROOT
from app.schemas.trips import TripPlan

SHARE_DIR = PROJECT_ROOT / "data" / "trips" / "shares"
TTL_SECONDS = 14 * 24 * 3600
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{10,40}$")

_MODE_TEXT = {"driving": "驾车", "walking": "步行", "bicycling": "骑行"}

_STYLE = """
:root{--navy:#16263f;--ink:#1d2b3a;--muted:#6b7a8c;--line:#d8e1ee;--bg:#f6f9fd}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
main{max-width:760px;margin:0 auto;padding:32px 20px 48px}
.kicker{font-size:11px;letter-spacing:.24em;color:var(--muted);margin:0 0 8px}
h1{font-family:Georgia,"Times New Roman",serif;font-size:36px;line-height:1.15;margin:0 0 10px}
.meta{font-size:12.5px;color:var(--muted);margin:0 0 22px}
figure{margin:0 0 22px}
figure img{width:100%;border-radius:14px;display:block;box-shadow:0 12px 30px rgba(22,38,63,.12)}
figcaption{font-size:11px;color:var(--muted);margin-top:7px}
section{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px 18px 14px;margin:0 0 18px}
h2{font-size:18px;margin:0 0 12px}
section img{width:100%;border-radius:10px;display:block;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:11px;color:var(--muted);font-weight:600}
td.time{white-space:nowrap;color:var(--muted);width:64px}
footer{margin-top:26px;font-size:11px;color:var(--muted);text-align:center;line-height:1.8}
@media(max-width:480px){h1{font-size:29px}main{padding:24px 14px 36px}}
"""


def _esc(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _data_uri(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_html(
    plan: TripPlan, overview: Path | None, day_maps: list[Path | None], expires_at: str
) -> str:
    meta = [
        x for x in [plan.city, _MODE_TEXT.get(plan.mode, plan.mode), f"{len(plan.days)} 天"] if x
    ]
    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{_esc(plan.title)} · 行程分享</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        '<p class="kicker">TRAVEL ITINERARY</p>',
        f"<h1>{_esc(plan.title)}</h1>",
        f'<p class="meta">{_esc(" · ".join(meta))}</p>',
    ]

    overview_uri = _data_uri(overview)
    if overview_uri:
        parts.append(
            f'<figure><img src="{overview_uri}" alt="路线总览"><figcaption>路线总览</figcaption></figure>'
        )

    if plan.origin:
        parts.append(f'<p class="meta">出发地：{_esc(plan.origin)}</p>')

    for index, day in enumerate(plan.days, start=1):
        title = day.title or f"第 {index} 天"
        if day.date:
            title = f"{title}（{day.date}）"
        parts.append("<section>")
        parts.append(f"<h2>{_esc(title)}</h2>")
        day_uri = _data_uri(day_maps[index - 1] if index - 1 < len(day_maps) else None)
        if day_uri:
            parts.append(f'<img src="{day_uri}" alt="{_esc(title)} 路线">')
        if day.stops:
            parts.append(
                "<table><thead><tr><th>时间</th><th>地点</th><th>备注</th></tr></thead><tbody>"
            )
            for stop in day.stops:
                parts.append(
                    "<tr>"
                    f'<td class="time">{_esc(stop.time or "—")}</td>'
                    f"<td>{_esc(stop.name)}</td>"
                    f"<td>{_esc(stop.note or '—')}</td>"
                    "</tr>"
                )
            parts.append("</tbody></table>")
        else:
            parts.append('<p class="meta">（未安排地点）</p>')
        parts.append("</section>")

    parts.append(
        f"<footer>只读分享 · 有效期至 {_esc(expires_at)}<br>由 Ocean 旅行助手生成，行程信息仅供参考</footer>"
    )
    parts.append("</main></body></html>")
    return "\n".join(parts)


def save_share(
    plan: TripPlan, overview: Path | None, day_maps: list[Path | None]
) -> tuple[str, str]:
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(12)
    created = int(time.time())
    expires_ts = created + TTL_SECONDS
    expires_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_ts))

    html = render_html(plan, overview, day_maps, expires_at)
    (SHARE_DIR / f"{token}.html").write_text(html, encoding="utf-8")
    (SHARE_DIR / f"{token}.json").write_text(
        json.dumps(
            {
                "created_at": created,
                "expires_at": expires_ts,
                "title": plan.title,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return token, expires_at


def load_share(token: str) -> str | None:
    if not _TOKEN_RE.fullmatch(token or ""):
        return None
    meta_path = SHARE_DIR / f"{token}.json"
    html_path = SHARE_DIR / f"{token}.html"
    if not meta_path.is_file() or not html_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if int(meta.get("expires_at") or 0) < int(time.time()):
        try:
            meta_path.unlink(missing_ok=True)
            html_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        return html_path.read_text(encoding="utf-8")
    except OSError:
        return None
