"""图片后端：调用 Infinite Canvas 的代理接口。

Infinite Canvas 的响应结构与 OpenAI 不同，统一为 {code, data, msg}，
且业务失败时 HTTP 状态码仍是 200——必须判 code，不能只看状态码。
"""

from __future__ import annotations

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


class CustomImageBackend(MediaBackend):
    """通过可配置的 HTTP 端点生图（当前指向 Infinite Canvas）。"""

    kind = MediaKind.IMAGE
    name = "custom_image"

    def __init__(
        self,
        endpoint: str,
        token: str,
        model: str,
        size: str = "2K",
        response_format: str = "url",
        watermark: bool = False,
        timeout_sec: float = 300.0,
    ) -> None:
        self._endpoint = endpoint
        self._token = token
        self._model = model
        self._size = size
        self._response_format = response_format
        self._watermark = watermark
        self._timeout = timeout_sec

    def available(self) -> bool:
        return bool(self._endpoint and self._token)

    def unavailable_reason(self) -> str:
        if not self._endpoint:
            return "未配置 CUSTOM_IMAGE_ENDPOINT"
        if not self._token:
            return "未配置图片鉴权：请设置 CUSTOM_IMAGE_TOKEN，或配置 ARK_API_KEY（会自动复用）"
        return ""

    async def generate(self, req: MediaRequest) -> MediaResult:
        if not self.available():
            raise MediaBackendMissing(self.unavailable_reason())

        # 按 OpenAI Images 风格构造请求体。
        # 火山方舟（Seedream）就是这套形状：需要 size 与 response_format；
        # 若改指 Infinite Canvas，按其前端抓到的请求调整下面的 payload 即可。
        # response_format 固定为 url：b64_json 需要本机落盘，暂不支持。
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": self._build_prompt(req),
            "n": 1,
            "size": self._size,
            "response_format": self._response_format,
            # 火山方舟的 watermark 默认是 true（右下角「AI生成」字样）。
            # 必须显式传，否则用户会莫名拿到带水印的图。
            "watermark": self._watermark,
        }
        if req.ref_image:
            payload["image"] = req.ref_image

        headers = {
            "Authorization": self._token,
            "Content-Type": "application/json",
        }

        # trust_env=False：本地服务不走系统代理
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            try:
                resp = await client.post(self._endpoint, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise MediaError("MEDIA_UPSTREAM_TIMEOUT", f"生图请求超时：{exc}") from exc
            except httpx.HTTPError as exc:
                raise MediaError("MEDIA_UPSTREAM_ERROR", f"生图请求失败：{exc}") from exc

        body = self._parse_body(resp)
        url = self._extract_url(body)
        if not url:
            raise UpstreamError(f"上游未返回图片地址，原始响应：{body!r}"[:500])
        return MediaResult(status=TaskStatus.SUCCEEDED, result_url=url, raw=body)

    # ---------- 内部方法 ----------

    @staticmethod
    def _build_prompt(req: MediaRequest) -> str:
        if req.style:
            return f"{req.prompt}，风格：{req.style}"
        return req.prompt

    @staticmethod
    def _parse_body(resp: httpx.Response) -> dict[str, Any]:
        """解析响应体，处理 Infinite Canvas 的 {code, data, msg} 约定。"""
        try:
            body = resp.json()
        except ValueError as exc:
            raise UpstreamError(
                f"上游返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}"
            ) from exc

        if not isinstance(body, dict):
            raise UpstreamError(f"上游响应结构异常：{body!r}"[:300])

        # OpenAI 风格（无 code 字段）直接放行，HTTP 状态码即结果
        if "code" not in body:
            if resp.status_code >= 400:
                raise UpstreamError(f"HTTP {resp.status_code}：{body!r}"[:300])
            return body

        # Infinite Canvas 风格：code == 0 才算成功
        if body.get("code") != 0:
            raise UpstreamError(str(body.get("msg") or body)[:300])
        data = body.get("data")
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _extract_url(body: dict[str, Any]) -> str | None:
        """从多种可能的响应形状里取图片地址。"""
        items = body.get("data")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return first.get("url") or first.get("image_url")
            if isinstance(first, str):
                return first

        for key in ("url", "image_url", "result_url"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value

        images = body.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                return first.get("url")
            if isinstance(first, str):
                return first

        return None
