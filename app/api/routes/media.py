"""媒体生成路由，对齐 SPEC 契约 2.5。"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Request, Response

from app.core.config import PROJECT_ROOT
from app.core.errors import ApiError
from app.core.identity import resolve_user_id
from app.media.base import MediaKind, MediaRequest
from app.media.service import MediaService
from app.media.templates import TEMPLATES
from app.media.uploads import ALLOWED_MIME, MAX_UPLOAD_BYTES, UploadStore
from app.schemas.media import (
    ImageRequest,
    ImageTemplateItem,
    ImageTemplateList,
    TaskAccepted,
    TaskStatusResponse,
    UploadOut,
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


# ---------- 用户上传的图片（多模态输入） ----------


def _uploads(request: Request) -> UploadStore:
    return request.app.state.uploads


@router.post("/uploads", response_model=UploadOut)
async def create_upload(request: Request) -> UploadOut:
    """接收一张图片，规范化后落盘。

    直读请求体而不是 multipart：一次只传一张，省一个依赖和一个解析器。
    前端用 ``xhr.send(file)`` 发，``Content-Type`` 就是文件本身的类型。
    """
    declared = int(request.headers.get("content-length") or 0)
    if declared > MAX_UPLOAD_BYTES:
        # 先看 Content-Length 再读体：几十 MB 的请求不该先进内存再被拒
        raise ApiError(
            "UPLOAD_TOO_LARGE",
            f"图片超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限",
        )

    raw = await request.body()
    if not raw:
        raise ApiError("INVALID_REQUEST", "请求体为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError(
            "UPLOAD_TOO_LARGE",
            f"图片超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限",
        )

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    filename = _filename_of(request)
    # 手机相册偶尔给 application/octet-stream，此时按扩展名兜底
    unknown_but_named = content_type == "application/octet-stream" and _looks_like_image(filename)
    if content_type and content_type not in ALLOWED_MIME and not unknown_but_named:
        raise ApiError("UPLOAD_TYPE_NOT_ALLOWED", f"不支持的图片类型：{content_type}")

    try:
        item = _uploads(request).save_image(raw, resolve_user_id(request), filename)
    except ValueError as exc:
        # Pillow 解析失败或超限，都归到「这个文件当图片用不了」
        raise ApiError("UPLOAD_TYPE_NOT_ALLOWED", str(exc)) from None

    return UploadOut(
        upload_id=item.id,
        url=f"/api/v1/media/uploads/{item.id}",
        width=item.width,
        height=item.height,
        bytes=item.bytes,
        format=item.ext.lstrip("."),
    )


@router.get("/uploads/{upload_id}")
async def get_upload(upload_id: str, request: Request) -> Response:
    """取回原图（已规范化）。前端缩略图与气泡里的预览都走这里。

    **不校验设备 ID**，这是刻意的：``<img src>`` 带不了自定义请求头，
    校验了就等于让页面里的图全都 403。32 位十六进制随机 id 本身就是凭证，
    和 S3 预签名链接一个思路——拿到链接就能看，拿不到就猜不着。
    写操作（DELETE）仍然校验归属，因为那是 XHR，能带头。
    """
    item = _uploads(request).get(upload_id)
    if item is None:
        raise ApiError("UPLOAD_NOT_FOUND", "图片不存在或已过期")
    path = _uploads(request).path_of(item)
    if not path.is_file():
        raise ApiError("UPLOAD_NOT_FOUND", "图片文件已丢失")
    return Response(
        content=path.read_bytes(),
        media_type="image/png" if item.ext == ".png" else "image/jpeg",
        headers={
            # 内容按 id 固化，可以放心让浏览器缓存，省掉每次刷新的重复下载
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": 'inline; filename="upload-%s%s"' % (item.id[:8], item.ext),
        },
    )


@router.delete("/uploads/{upload_id}", status_code=204)
async def delete_upload(upload_id: str, request: Request) -> Response:
    """用户把附件从输入框里删掉时调用，避免落一堆没人用的图。"""
    if not _uploads(request).delete(upload_id, resolve_user_id(request)):
        raise ApiError("UPLOAD_NOT_FOUND", "图片不存在或已过期")
    return Response(status_code=204)


def _filename_of(request: Request) -> str:
    # HTTP 头只能是 latin-1，中文文件名必须由前端 encodeURIComponent 之后再发，
    # 这里 unquote 回来。不做这一步的话，浏览器发「海边.png」会直接被 XHR 拒掉。
    raw = unquote(request.headers.get("x-file-name") or "")
    # 只保留文件名本身，防止 ../ 之类的东西混进来
    return Path(raw.replace("\\", "/")).name[:120]


def _looks_like_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
