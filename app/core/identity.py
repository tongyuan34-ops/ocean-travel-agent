"""请求身份解析：目前只有设备维度，没有账号体系。

前端在 localStorage 生成一个 UUID 作为设备 ID，随每个请求带 `X-Device-ID`。
缺失或格式非法时统一落到 `default`，保证单人使用时不出现"记忆丢失"。
"""

from __future__ import annotations

import re

from fastapi import Request

DEFAULT_USER_ID = "default"
_DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def resolve_user_id(request: Request) -> str:
    """从请求头解析设备 ID。"""
    raw = (request.headers.get("X-Device-ID") or "").strip()
    return raw if _DEVICE_RE.match(raw) else DEFAULT_USER_ID
