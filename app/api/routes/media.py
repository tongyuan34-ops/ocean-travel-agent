"""媒体生成路由，对齐 SPEC 契约 2.5。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.errors import ApiError
from app.media.base import MediaKind, MediaRequest
from app.media.service import MediaService
from app.media.templates import TEMPLATES
from app.schemas.media import (
    ImageRequest,
    ImageTemplateItem,
    ImageTemplateList,
    TaskAccepted,
    TaskStatusResponse,
    VideoRequest,
)

router = APIRouter(prefix="/media", tags=["media"])


def _service(request: Request) -> MediaService:
    return request.app.state.media


@router.get("/templates", response_model=ImageTemplateList)
async def list_templates() -> ImageTemplateList:
    """生图提示词模板库，供前端渲染模板选择器。"""
    return ImageTemplateList(
        items=[ImageTemplateItem(**item.to_dict()) for item in TEMPLATES]
    )


@router.post("/images", status_code=202, response_model=TaskAccepted)
async def create_image(payload: ImageRequest, request: Request) -> TaskAccepted:
    """提交生图任务，立即返回 pending。"""
    task = await _service(request).submit(
        MediaRequest(
            kind=MediaKind.IMAGE,
            prompt=payload.prompt,
            style=payload.style,
            ref_image=payload.ref_image,
        )
    )
    return TaskAccepted(task_id=task.id, status=task.status.value)


@router.post("/videos", status_code=202, response_model=TaskAccepted)
async def create_video(payload: VideoRequest, request: Request) -> TaskAccepted:
    """提交生视频任务。带 ref_image 即为图生视频。"""
    task = await _service(request).submit(
        MediaRequest(
            kind=MediaKind.VIDEO,
            prompt=payload.prompt,
            ref_image=payload.ref_image,
            duration=payload.duration,
        )
    )
    return TaskAccepted(task_id=task.id, status=task.status.value)


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str, request: Request) -> TaskStatusResponse:
    """查询任务状态与结果。"""
    task = await _service(request).get(task_id)
    if task is None:
        raise ApiError("TASK_NOT_FOUND", f"任务不存在或已过期：{task_id}")
    return TaskStatusResponse(**task.to_dict())
