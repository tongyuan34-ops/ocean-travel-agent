"""会话与消息模型，对齐 SPEC 契约 2.1 / 2.2。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MessageRequest(BaseModel):
    message: str = Field(default="", max_length=4000, description="用户输入")
    # 附图。允许只发图不写字（「这张图里是什么」），所以 message 不是必填，
    # 但两者不能都空——校验放在路由里做，错误码统一走 INVALID_REQUEST
    attachments: list[str] = Field(
        default_factory=list, max_length=6, description="图片 upload_id 列表"
    )


class SessionCreated(BaseModel):
    session_id: str
    created_at: str


class SessionItem(BaseModel):
    """会话列表里的一行。

    标题由后端从首条用户消息里截，不在前端拼：前端只看得见 localStorage 里
    自己记的那份，换端口/换设备就空了，标题会退化成「新的旅程」。
    """

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class SessionList(BaseModel):
    items: list[SessionItem]


class MessageItem(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    media_task_ids: list[str] = Field(default_factory=list)
    # 本条回答的用量与耗时，用于在消息下方展示；用户消息为空 / 0
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    # 用户消息附带的附件 id。刷新后据此重建气泡里的缩略图 / 文件卡片
    attachments: list[str] = Field(default_factory=list)
    # 附件 id → {kind, filename, ext, bytes}。只有 id 的话前端分不清该画缩略图
    # 还是画文件卡片（文档没有图可显示）
    attachment_meta: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 本轮自检告警。刷新后仍要显示，否则用户回头翻记录会以为当时成功了
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class MessageList(BaseModel):
    items: list[MessageItem]
    # 本会话当前的对话风格。前端刷新页面后靠它恢复选中项——
    # 只存在浏览器本地的话，换设备或清缓存后就和后端对不上了
    persona: str = "concierge"
    # 本会话固定使用的技能 key；空表示没指定，由模型按触发词判断
    skill: str = ""


class PersonaItem(BaseModel):
    """一种对话风格。不暴露 prompt 正文，那是内部实现。"""

    key: str
    name: str
    latin: str
    tagline: str


class PersonaList(BaseModel):
    items: list[PersonaItem]
    default: str


class PersonaUpdate(BaseModel):
    persona: str = Field(min_length=1, max_length=32, description="风格 key")


class PersonaOut(BaseModel):
    persona: str


class SkillItem(BaseModel):
    """一个可用技能。不暴露完整步骤正文——那是内部实现，前端只需要展示字段。"""

    key: str
    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    has_tools: bool = False
    tools: list[str] = Field(default_factory=list)


class SkillList(BaseModel):
    items: list[SkillItem]
    # 技能目录为空时前端据此隐藏入口，而不是展示一个空面板
    enabled: bool = True


class SkillUpdate(BaseModel):
    # 空串表示「不指定技能」。允许为空是清空操作的唯一表达方式
    skill: str = Field(default="", max_length=64, description="技能 key，空串表示不指定")


class SkillOut(BaseModel):
    skill: str


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
