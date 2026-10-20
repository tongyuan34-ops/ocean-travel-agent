"""媒体任务服务：对外提供 SPEC 契约 2.5 要求的异步任务语义。

职责：
- 创建任务并立即返回 pending（前端拿到 task_id 即可展示占位卡片）
- 后台执行真实生成，过程中更新 running / succeeded / failed
- 保存 our_id 与后端的关系，不把上游 task id 暴露给前端
- 任务仅存内存，进程重启即丢失（骨架阶段的已知限制）
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.config import Settings
from app.media.ark_video import ArkVideoBackend
from app.media.base import (
    MediaBackend,
    MediaBackendMissing,
    MediaError,
    MediaKind,
    MediaRequest,
    TaskStatus,
)
from app.media.custom_image import CustomImageBackend

# 任务状态变化回调，签名 (task) -> None，后续接 SSE 推送
UpdateHook = Callable[["MediaTask"], Awaitable[None] | None]

# 当前会话可用的媒体服务。
# 工具层必须用它而不是自己 new 一个：任务存在 service 实例里，
# 若工具另建实例，返回的 task_id 在 /api/v1/media/tasks/{id} 上查不到。
current_media: ContextVar["MediaService | None"] = ContextVar(
    "current_media", default=None
)


@dataclass
class MediaTask:
    """一个媒体任务。字段与 SPEC 契约 2.5 的响应结构对齐。"""

    id: str
    kind: MediaKind
    status: TaskStatus
    prompt: str
    style: str | None = None
    ref_image: str | None = None
    duration: int | None = None
    result_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """按契约输出：{task_id, status, result_url, error}"""
        return {
            "task_id": self.id,
            "status": self.status.value,
            "result_url": self.result_url,
            "error": self.error,
        }


class MediaService:
    def __init__(
        self,
        image_backend: MediaBackend | None,
        video_backend: MediaBackend | None,
        ttl_sec: int = 3600,
        on_update: UpdateHook | None = None,
    ) -> None:
        self._backends: dict[MediaKind, MediaBackend | None] = {
            MediaKind.IMAGE: image_backend,
            MediaKind.VIDEO: video_backend,
        }
        self._tasks: dict[str, MediaTask] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl_sec
        self._on_update = on_update

    # ---------- 对外接口 ----------

    def capabilities(self) -> dict[str, Any]:
        """供 GET /api/v1/system/capabilities 使用。"""
        return {
            "image_gen": self._is_ready(MediaKind.IMAGE),
            "video_gen": self._is_ready(MediaKind.VIDEO),
        }

    def unavailable_reasons(self) -> dict[str, str]:
        """不可用原因，便于启动时打印与排障。"""
        reasons: dict[str, str] = {}
        for kind, backend in self._backends.items():
            if backend is None:
                reasons[kind.value] = "未配置该类型后端"
            elif not backend.available():
                reasons[kind.value] = backend.unavailable_reason()
        return reasons

    async def submit(self, req: MediaRequest) -> MediaTask:
        """创建任务并立即返回；真实生成在后台进行。"""
        backend = self._backends.get(req.kind)
        if backend is None:
            raise MediaBackendMissing(f"未配置 {req.kind.value} 后端")
        if not backend.available():
            raise MediaBackendMissing(backend.unavailable_reason())

        task = MediaTask(
            id=str(uuid4()),
            kind=req.kind,
            status=TaskStatus.PENDING,
            prompt=req.prompt,
            style=req.style,
            ref_image=req.ref_image,
            duration=req.duration,
        )
        async with self._lock:
            self._prune_locked()
            self._tasks[task.id] = task

        asyncio.create_task(self._run(task, req, backend))
        return task

    async def get(self, task_id: str) -> MediaTask | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def aclose(self) -> None:
        """等待所有进行中的任务收尾（测试脚本用）。"""
        while True:
            async with self._lock:
                pending = [
                    t for t in self._tasks.values()
                    if t.status in {TaskStatus.PENDING, TaskStatus.RUNNING}
                ]
            if not pending:
                return
            await asyncio.sleep(0.5)

    # ---------- 内部实现 ----------

    def _is_ready(self, kind: MediaKind) -> bool:
        backend = self._backends.get(kind)
        return bool(backend and backend.available())

    async def _run(
        self, task: MediaTask, req: MediaRequest, backend: MediaBackend
    ) -> None:
        await self._update(task, status=TaskStatus.RUNNING)
        try:
            result = await backend.generate(req)
        except MediaError as exc:
            # 上游错误码原样透传，不吞掉原因
            await self._update(task, status=TaskStatus.FAILED, error=exc.message)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免后台任务静默死亡
            await self._update(
                task, status=TaskStatus.FAILED, error=f"未预期错误：{exc}"
            )
        else:
            await self._update(
                task, status=result.status, result_url=result.result_url, error=result.error
            )

    async def _update(
        self,
        task: MediaTask,
        *,
        status: TaskStatus,
        result_url: str | None = None,
        error: str | None = None,
    ) -> None:
        task.status = status
        if result_url is not None:
            task.result_url = result_url
        if error is not None:
            task.error = error
        task.updated_at = time.time()

        hook = self._on_update
        if hook is not None:
            outcome = hook(task)
            if asyncio.iscoroutine(outcome):
                await outcome

    def _prune_locked(self) -> None:
        """清理过期任务，避免内存无限增长。调用前需持锁。"""
        if self._ttl <= 0:
            return
        now = time.time()
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}
            and now - task.updated_at > self._ttl
        ]
        for task_id in expired:
            self._tasks.pop(task_id, None)


def build_media_service(
    settings: Settings, on_update: UpdateHook | None = None
) -> MediaService:
    """按 .env 配置组装媒体服务。未配置的类型置为 None。"""
    image_backend: MediaBackend | None = None
    if settings.image_backend == "custom":
        image_backend = CustomImageBackend(
            endpoint=settings.custom_image_endpoint,
            token=settings.custom_image_token,
            model=settings.custom_image_model,
            size=settings.custom_image_size,
            response_format=settings.custom_image_response_format,
            watermark=settings.custom_image_watermark,
        )

    video_backend: MediaBackend | None = None
    if settings.video_backend == "ark":
        video_backend = ArkVideoBackend(
            api_key=settings.ark_api_key,
            base_url=settings.ark_base_url,
            model=settings.ark_video_model,
            ratio=settings.ark_video_ratio,
            generate_audio=settings.ark_video_generate_audio,
            watermark=settings.ark_video_watermark,
            poll_initial_sec=settings.ark_video_poll_initial_sec,
            poll_max_sec=settings.ark_video_poll_max_sec,
            timeout_sec=settings.ark_video_timeout_sec,
        )

    return MediaService(
        image_backend=image_backend,
        video_backend=video_backend,
        ttl_sec=settings.media_task_ttl_sec,
        on_update=on_update,
    )
