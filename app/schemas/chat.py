"""会话与消息模型，对齐 SPEC 契约 2.1 / 2.2。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="用户输入")


class SessionCreated(BaseModel):
    session_id: str
    created_at: str


class MessageItem(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    media_task_ids: list[str] = Field(default_factory=list)


class MessageList(BaseModel):
    items: list[MessageItem]
