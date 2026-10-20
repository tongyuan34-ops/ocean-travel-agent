"""存储层共用的数据模型。

放在 storage 层而不是 services 层：存储实现需要知道持久化对象的形状，
依赖方向应为 services → storage，不能反过来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    id: str
    role: str  # user | assistant
    content: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatMessage":
        return cls(
            id=str(data.get("id", "")),
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class ChatSession:
    id: str
    created_at: str
    messages: list[ChatMessage] = field(default_factory=list)
    # 滚动摘要，以及已被折叠进摘要的消息条数（messages 的下标）
    summary: str = ""
    summarized_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "summary": self.summary,
            "summarized_count": self.summarized_count,
            "messages": [item.to_dict() for item in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatSession":
        return cls(
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", "")),
            summary=str(data.get("summary", "")),
            summarized_count=int(data.get("summarized_count", 0)),
            messages=[ChatMessage.from_dict(item) for item in data.get("messages") or []],
        )
