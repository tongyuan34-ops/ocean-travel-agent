"""只读分享页：不需要登录态，也不包含任何会话标识。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.services.trip_share import load_share

router = APIRouter(tags=["share"])

_NOT_FOUND = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>分享已失效</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f6f9fd;color:#1d2b3a;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{text-align:center;padding:32px}h1{font-size:24px;margin:0 0 10px}p{color:#6b7a8c;font-size:13px}</style>
</head><body><main><h1>分享已失效</h1><p>链接不存在或已超过有效期。</p></main></body></html>"""


@router.get("/share/{token}")
async def share_page(token: str) -> HTMLResponse:
    html = load_share(token)
    if html is None:
        return HTMLResponse(_NOT_FOUND, status_code=404)
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )
