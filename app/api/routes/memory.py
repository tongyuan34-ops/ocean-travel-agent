"""用户偏好路由：查看、确认、删除与候选管理。

所有接口按设备隔离（X-Device-ID）。写入只发生在用户显式确认之后，
后端不会把自动抽取的结果直接写进正式偏好。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.identity import resolve_user_id
from app.schemas.memory import (
    MemoryItemOut,
    MemoryItemsRequest,
    MemoryList,
    MemorySuggestion,
    MemorySuggestionList,
)
from app.services.profile import ProfileService

router = APIRouter(prefix="/memory", tags=["memory"])


def _service(request: Request) -> ProfileService:
    return request.app.state.profile


def _to_list(data: dict) -> MemoryList:
    return MemoryList(
        items=[
            MemoryItemOut(
                id=str(item.get("id") or ""),
                category=str(item.get("category") or "其他"),
                text=str(item.get("text") or ""),
                created_at=str(item.get("created_at") or ""),
            )
            for item in data.get("items") or []
            if item.get("text")
        ],
        updated_at=str(data.get("updated_at") or ""),
    )


@router.get("", response_model=MemoryList)
async def get_memory(request: Request) -> MemoryList:
    """读取当前设备的正式偏好。"""
    return _to_list(await _service(request).get(resolve_user_id(request)))


@router.post("/items", response_model=MemoryList)
async def add_memory(payload: MemoryItemsRequest, request: Request) -> MemoryList:
    """用户确认后写入偏好（判重、最多 20 条）。"""
    user_id = resolve_user_id(request)
    data = await _service(request).add(user_id, [item.model_dump() for item in payload.items])
    await _service(request).dismiss_suggestions(user_id)
    return _to_list(data)


@router.delete("/items/{item_id}", status_code=204)
async def delete_memory(item_id: str, request: Request) -> None:
    """删除一条偏好。"""
    await _service(request).remove(resolve_user_id(request), item_id)


@router.delete("", status_code=204)
async def clear_memory(request: Request) -> None:
    """清空当前设备的全部偏好。"""
    user_id = resolve_user_id(request)
    await _service(request).clear(user_id)
    await _service(request).dismiss_suggestions(user_id)


@router.get("/suggestions", response_model=MemorySuggestionList)
async def get_suggestions(request: Request) -> MemorySuggestionList:
    """读取最近一轮抽取出的待确认候选。"""
    items = await _service(request).get_suggestions(resolve_user_id(request))
    return MemorySuggestionList(
        items=[
            MemorySuggestion(
                id=str(item.get("id") or ""),
                category=str(item.get("category") or "其他"),
                text=str(item.get("text") or ""),
            )
            for item in items
            if item.get("text")
        ]
    )


@router.delete("/suggestions", status_code=204)
async def dismiss_suggestions(request: Request) -> None:
    """忽略候选，不写入偏好。"""
    await _service(request).dismiss_suggestions(resolve_user_id(request))
