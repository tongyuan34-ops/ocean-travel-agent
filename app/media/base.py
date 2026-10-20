"""媒体后端抽象：统一图片与视频的调用契约。

设计要点：
- 每个后端只需实现一个 `generate()` 协程：内部可包含创建 + 轮询等全部等待，
  最终返回终态。外部的「异步任务」语义由 service.py 统一承载，
  这样前端看到的始终是 SPEC 契约 2.5 的 task_id 轮询模型。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MediaError(Exception):
    """媒体后端错误。code 对应 SPEC 契约错误码，直接透传给前端。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class MediaBackendMissing(MediaError):
    """未配置后端。"""

    def __init__(self, detail: str) -> None:
        super().__init__("MEDIA_BACKEND_MISSING", detail)


class UpstreamError(MediaError):
    """上游返回错误，原样透传原因，不得吞掉。"""

    def __init__(self, message: str) -> None:
        super().__init__("MEDIA_UPSTREAM_ERROR", message)


@dataclass
class MediaRequest:
    """一次生成请求，字段与 SPEC 契约 2.5 对齐。"""

    kind: MediaKind
    prompt: str
    style: str | None = None
    ref_image: str | None = None
    duration: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaResult:
    status: TaskStatus
    result_url: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MediaBackend(abc.ABC):
    """媒体后端基类。"""

    name: str = "base"
    kind: MediaKind = MediaKind.IMAGE

    def available(self) -> bool:
        """依赖是否就绪，决定 capabilities 中该项是否为 true。"""
        return False

    def unavailable_reason(self) -> str:
        """不可用原因，供前端展示。"""
        return "未配置"

    @abc.abstractmethod
    async def generate(self, req: MediaRequest) -> MediaResult:
        """执行一次生成并返回终态结果。

        内部可以完成创建 + 轮询的完整等待；失败时抛 MediaError。
        """
        raise NotImplementedError
