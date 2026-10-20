"""检索层的统一数据模型（SPEC 4.7）。

任何数据源（必应、网页正文、后续的社媒适配器）产出的结果一律归一化为 Doc。
去重、排序、来源引用都只基于 Doc，**不得针对某个源做特判**——
否则每接一个源就要改一遍排序逻辑，最终无法维护。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SourceKind(str, Enum):
    """数据源类型。沿用 SPEC 4.7 的取值。"""

    GENERAL_CN = "general_cn"
    GENERAL_GLOBAL = "general_global"
    SOCIAL_CN = "social_cn"
    CODE = "code"
    ACADEMIC = "academic"
    WEB = "web"
    RECRUIT = "recruit"


@dataclass(slots=True)
class Doc:
    """归一化后的单条检索结果。

    mock=True 表示这是降级占位结果（源不可用或源内失败），
    前端必须据此标注「降级」，不得当作真实数据展示。
    """

    title: str
    url: str
    snippet: str = ""
    content: str = ""
    source: str = ""          # 适配器 name，如 bing_cn
    source_kind: str = ""     # SourceKind 的值
    platform: str = ""        # 前端展示名，如「必应（中国）」
    author: str = ""
    published_at: str = ""
    score: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)
    mock: bool = False

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化的结构，字段名与 SPEC 一致。"""
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "content": self.content,
            "source": self.source,
            "source_kind": self.source_kind,
            "platform": self.platform,
            "author": self.author,
            "published_at": self.published_at,
            "score": self.score,
            "extras": self.extras,
            "mock": self.mock,
        }


def source_kind_value(kind: SourceKind | str) -> str:
    """取 SourceKind 的字符串值，兼容直接传入字符串。"""
    return kind.value if isinstance(kind, SourceKind) else str(kind)
