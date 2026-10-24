"""媒体相关模型，对齐 SPEC 契约 2.5。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000, description="生成提示词")
    style: str | None = Field(
        default=None, max_length=200, description="风格，如 checkin / poster / note"
    )
    ref_image: str | None = Field(default=None, description="参考图 URL")


class VideoRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000, description="生成提示词")
    duration: int = Field(default=5, ge=1, le=30, description="时长（秒）")
    ref_image: str | None = Field(default=None, description="首帧参考图 URL；传入即为图生视频")


class TaskAccepted(BaseModel):
    """创建任务的响应，HTTP 202。"""

    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    """任务状态查询响应。"""

    task_id: str
    status: str
    result_url: str | None = None
    error: str | None = None


class ImageTemplateItem(BaseModel):
    """一个生图模板。"""

    key: str
    label: str
    scene: str
    style: str
    composition: str
    lighting: str
    example: str


class ImageTemplateList(BaseModel):
    items: list[ImageTemplateItem]
