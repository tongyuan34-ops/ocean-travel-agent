"""工具清单模型，对齐 SPEC 契约 2.2 的 GET /api/v1/tools。"""

from __future__ import annotations

from pydantic import BaseModel


class ToolItem(BaseModel):
    name: str
    category: str
    enabled: bool
    requires_permission: bool
    description: str


class ToolList(BaseModel):
    items: list[ToolItem]
