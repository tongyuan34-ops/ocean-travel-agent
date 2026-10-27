"""权限裁决路由，对齐 SPEC 契约 2.4。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.agent.permissions import Decision
from app.core.errors import ApiError
from app.schemas.permissions import DecisionRequest, DecisionResponse

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.post("/{request_id}/decision", response_model=DecisionResponse)
async def decide(request_id: str, payload: DecisionRequest, request: Request) -> DecisionResponse:
    """前端对 permission_request 的裁决。

    未找到或已处理的 request_id 一律拒绝，不做任何默认放行。

    裁决不再需要「找到持有该请求的那个 broker」——多进程下这次 POST
    可能落在任何一台机器上。裁决写进共享通道，由真正在等的那一端取走。
    """
    channel = request.app.state.decisions
    decision = Decision(granted=payload.granted, remember=payload.remember)
    if not await channel.decide(request_id, decision):
        raise ApiError("INVALID_REQUEST", f"该权限请求已处理、已超时或不存在：{request_id}")

    return DecisionResponse(request_id=request_id, granted=payload.granted)
