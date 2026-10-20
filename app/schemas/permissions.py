"""权限裁决模型，对齐 SPEC 契约 2.4。"""

from __future__ import annotations

from pydantic import BaseModel


class DecisionRequest(BaseModel):
    granted: bool
    remember: bool = False


class DecisionResponse(BaseModel):
    request_id: str
    granted: bool
