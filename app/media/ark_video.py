"""视频后端：直连火山方舟 Seedance。

视频生成是异步过程：
    POST /contents/generations/tasks  → 返回任务 ID
    GET  /contents/generations/tasks/{id} → 轮询至 succeeded
    结果在 content.video_url（MP4）

不走 Infinite Canvas：它的 /api/v1/videos 要对接非 OpenAI 风格的路径，
只能靠其无公开文档的 parameterTranslation 转译，维护成本高。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.media.base import (
    MediaBackend,
    MediaBackendMissing,
    MediaError,
    MediaKind,
    MediaRequest,
    MediaResult,
    TaskStatus,
    UpstreamError,
)

_CREATE_PATH = "/contents/generations/tasks"


class ArkVideoBackend(MediaBackend):
    kind = MediaKind.VIDEO
    name = "ark_video"

    # 上游状态 → 本项目状态
    _STATUS_MAP = {
        "queued": TaskStatus.PENDING,
        "running": TaskStatus.RUNNING,
        "succeeded": TaskStatus.SUCCEEDED,
        "failed": TaskStatus.FAILED,
    }

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        ratio: str = "adaptive",
        generate_audio: bool = True,
        watermark: bool = False,
        poll_initial_sec: float = 3.0,
        poll_max_sec: float = 10.0,
        timeout_sec: float = 600.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._ratio = ratio
        self._generate_audio = generate_audio
        self._watermark = watermark
        self._poll_initial = poll_initial_sec
        self._poll_max = poll_max_sec
        self._timeout = timeout_sec

    def available(self) -> bool:
        return bool(self._api_key and self._model)

    def unavailable_reason(self) -> str:
        if not self._api_key:
            return "未配置 ARK_API_KEY"
        if not self._model:
            return "未配置 ARK_VIDEO_MODEL"
        return ""

    async def generate(self, req: MediaRequest) -> MediaResult:
        if not self.available():
            raise MediaBackendMissing(self.unavailable_reason())

        # 火山方舟是公网服务，正常走系统代理
        async with httpx.AsyncClient(timeout=60.0) as client:
            task_id = await self._create(client, req)
            return await self._poll(client, task_id)

    # ---------- 内部方法 ----------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _create(self, client: httpx.AsyncClient, req: MediaRequest) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": req.prompt}]
        if req.ref_image:
            # 有参考图即图生视频（首帧）
            content.append({"type": "image_url", "image_url": {"url": req.ref_image}})

        payload: dict[str, Any] = {
            "model": self._model,
            "content": content,
            "ratio": self._ratio,
            "duration": req.duration or 5,
            "generate_audio": self._generate_audio,
            "watermark": self._watermark,
        }

        try:
            resp = await client.post(
                f"{self._base_url}{_CREATE_PATH}", json=payload, headers=self._headers
            )
        except httpx.TimeoutException as exc:
            raise MediaError("MEDIA_UPSTREAM_TIMEOUT", f"创建视频任务超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise MediaError("MEDIA_UPSTREAM_ERROR", f"创建视频任务失败：{exc}") from exc

        body = _json_or_raise(resp)
        task_id = body.get("id")
        if not task_id:
            raise UpstreamError(f"未返回任务 ID，原始响应：{body!r}"[:300])
        return str(task_id)

    async def _poll(self, client: httpx.AsyncClient, task_id: str) -> MediaResult:
        deadline = time.monotonic() + self._timeout
        interval = self._poll_initial

        while True:
            try:
                resp = await client.get(
                    f"{self._base_url}{_CREATE_PATH}/{task_id}", headers=self._headers
                )
            except httpx.HTTPError as exc:
                raise MediaError("MEDIA_UPSTREAM_ERROR", f"查询视频任务失败：{exc}") from exc

            body = _json_or_raise(resp)
            raw_status = str(body.get("status") or "").lower()
            status = self._STATUS_MAP.get(raw_status)

            if status is TaskStatus.SUCCEEDED:
                video_url = (body.get("content") or {}).get("video_url")
                if not video_url:
                    raise UpstreamError("任务已成功但响应中没有 content.video_url")
                return MediaResult(
                    status=TaskStatus.SUCCEEDED, result_url=str(video_url), raw=body
                )

            if status is TaskStatus.FAILED:
                reason = body.get("error") or body.get("message") or body
                raise UpstreamError(str(reason)[:300])

            if time.monotonic() > deadline:
                raise MediaError(
                    "MEDIA_UPSTREAM_TIMEOUT",
                    f"视频任务等待超时（{self._timeout:.0f}s），task_id={task_id}",
                )

            await asyncio.sleep(interval)
            # 逐步退避，降低轮询频率
            interval = min(interval * 1.5, self._poll_max)


def _json_or_raise(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise UpstreamError(
            f"上游返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}"
        ) from exc

    if resp.status_code >= 400:
        message = body.get("error") if isinstance(body, dict) else None
        raise UpstreamError(f"HTTP {resp.status_code}：{message or body}"[:300])

    if not isinstance(body, dict):
        raise UpstreamError(f"上游响应结构异常：{body!r}"[:300])
    return body
