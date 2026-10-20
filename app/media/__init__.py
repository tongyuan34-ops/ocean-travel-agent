"""媒体层：图片与视频后端的适配与任务调度。"""

from app.media.base import (
    MediaBackend,
    MediaError,
    MediaKind,
    MediaRequest,
    MediaResult,
    TaskStatus,
)
from app.media.service import MediaService, MediaTask, build_media_service

__all__ = [
    "MediaBackend",
    "MediaError",
    "MediaKind",
    "MediaRequest",
    "MediaResult",
    "TaskStatus",
    "MediaService",
    "MediaTask",
    "build_media_service",
]
