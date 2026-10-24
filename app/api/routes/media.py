"""媒体生成路由，对齐 SPEC 契约 2.5。"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Request, Response

from app.core.config import PROJECT_ROOT
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
    return ImageTemplateList(items=[ImageTemplateItem(**item.to_dict()) for item in TEMPLATES])


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


_IMAGE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _safe_task_file(task_id: str) -> Path | None:
    """在项目 media/ 目录里定位 task-<id> 开头的本地成品图。

    这只兜底「任务已从内存过期，但有本地落盘成品」的场景，
    让前端仍能在页面内直接预览，而不是只给一个会过期的临时链接。
    """
    media_dir = PROJECT_ROOT / "media"
    if not media_dir.is_dir():
        return None
    try:
        for candidate in media_dir.iterdir():
            if candidate.is_file() and candidate.name.startswith(f"task-{task_id[:8]}"):
                return candidate
    except OSError:
        return None
    return None


def _image_headers(disposition: str, filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "public, max-age=600",
    }


@router.get("/tasks/{task_id}/image")
async def get_task_image(task_id: str, request: Request, download: bool = False) -> Response:
    """返回生成图本身，供页面内直接预览或下载。

    优先读项目 media/ 下的本地成品（重启后任务丢失也能看），
    其次用服务端代取上游临时链接，避免浏览器跨域与 24 小时过期问题。
    """
    if not _valid_task_id(task_id):
        raise ApiError("TASK_NOT_FOUND", f"任务不存在或已过期：{task_id}")

    local = _safe_task_file(task_id)
    if local is not None:
        disposition = "attachment" if download else "inline"
        return Response(
            content=local.read_bytes(),
            media_type=_media_type_of(local),
            headers=_image_headers(disposition, local.name),
        )

    task = await _service(request).get(task_id)
    if task is None or not task.result_url:
        raise ApiError("TASK_NOT_FOUND", f"任务不存在或已过期：{task_id}")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(task.result_url)
            body = resp.content
    except httpx.HTTPError:
        raise ApiError("UPSTREAM_ERROR", "生成图上游暂不可用，链接可能已过期") from None

    content_type = (resp.headers.get("content-type") or "image/jpeg").split(";")[0]
    ext = _IMAGE_EXT.get(content_type.lower(), ".jpg")
    _cache_image(task_id, ext, body)
    disposition = "attachment" if download else "inline"
    return Response(
        content=body,
        media_type=content_type,
        headers=_image_headers(disposition, f"travel-{task_id[:8]}{ext}"),
    )


def _cache_image(task_id: str, ext: str, body: bytes) -> None:
    """把上游成品落到 media/，后端重启、临时链接过期后仍可在页面内查看。"""
    if not body:
        return
    try:
        media_dir = PROJECT_ROOT / "media"
        media_dir.mkdir(exist_ok=True)
        (media_dir / f"task-{task_id[:8]}{ext}").write_bytes(body)
    except OSError:
        # 落盘失败不影响本次返回，只是重启后不能兜底
        pass


def _valid_task_id(task_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{8,64}", task_id or ""))


def _media_type_of(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
