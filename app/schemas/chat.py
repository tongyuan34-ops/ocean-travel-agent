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


class UsageOut(BaseModel):
    """会话累计用量。cost 为 None 表示价目表里没有这个模型（不是免费）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    cost: float | None = None
    currency: str = "CNY"
    # 是否含估算值：本地模型不返回 usage 时会退化为估算
    estimated: bool = False
    turns: int = 0
