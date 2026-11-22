"""运行指标：JSON 快照（人看）与 Prometheus 文本（机器抓）。

两个端点分开：

- ``GET /api/v1/metrics`` 给前端/运维脚本看，结构稳定、可读；
- ``GET /metrics`` 给 Prometheus / VictoriaMetrics 抓，必须是文本格式。

为什么不用同一个端点靠 ``Accept`` 头区分：抓取配置里多写一个 header
就多一处出错的地方，而且浏览器直接打开时看不到 JSON 会让人以为坏了。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.responses import PlainTextResponse

from app.core.metrics import metrics

router = APIRouter(tags=["system"])
text_router = APIRouter(tags=["system"])


@router.get("/metrics")
async def metrics_json() -> dict[str, Any]:
    """指标快照。字段见 ``app/core/metrics.py`` 的 ``snapshot()``。"""
    return metrics.snapshot()


@text_router.get("/metrics", include_in_schema=False)
async def metrics_text() -> PlainTextResponse:
    return PlainTextResponse(
        metrics.prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


__all__ = ["router", "text_router"]
