"""用户偏好接口模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MemoryItemIn(BaseModel):
    category: str = Field(default="其他", max_length=20)
    text: str = Field(min_length=1, max_length=120)


class MemoryItemsRequest(BaseModel):
    items: list[MemoryItemIn] = Field(min_length=1, max_length=10)


class MemoryItemOut(BaseModel):
    id: str
    category: str
    text: str
    created_at: str


class MemoryList(BaseModel):
    items: list[MemoryItemOut]
    updated_at: str = ""


class MemorySuggestion(BaseModel):
    id: str
    category: str
    text: str


class MemorySuggestionList(BaseModel):
    items: list[MemorySuggestion]
