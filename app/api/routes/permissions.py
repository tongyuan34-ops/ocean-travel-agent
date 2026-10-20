"""权限裁决路由，对齐 SPEC 契约 2.4。"""

from __future__ import annotations

from fastapi import APIRouter

from app.agent.permissions import find_broker
from app.core.errors import ApiError
from app.schemas.permissions import DecisionRequest, DecisionResponse

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.post("/{request_id}/decision", response_model=DecisionResponse)
async def decide(request_id: str, payload: DecisionRequest) -> DecisionResponse:
    """前端对 permission_request 的裁决。

    未找到或已处理的 request_id 一律拒绝，不做任何默认放行。
    """
    broker = find_broker(request_id)
    if broker is None:
        raise ApiError("INVALID_REQUEST", f"未找到待决的权限请求：{request_id}")

    if not broker.resolve(request_id, payload.granted, payload.remember):
        raise ApiError("INVALID_REQUEST", f"该权限请求已处理或已超时：{request_id}")

    return DecisionResponse(request_id=request_id, granted=payload.granted)
