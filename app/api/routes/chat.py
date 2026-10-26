"""会话与聊天路由（SSE），对齐 SPEC 契约 2.1 / 2.2。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.identity import resolve_user_id
from app.schemas.chat import (
    MessageItem,
    MessageList,
    MessageRequest,
    SessionCreated,
    UsageOut,
)
from app.services.chat import ChatService

router = APIRouter(prefix="/chat", tags=["chat"])


def _service(request: Request) -> ChatService:
    return request.app.state.chat


@router.post("/sessions", response_model=SessionCreated)
async def create_session(request: Request) -> SessionCreated:
    """创建会话。"""
    session = await _service(request).create_session()
    return SessionCreated(session_id=session.id, created_at=session.created_at)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> None:
    """删除会话及其消息。"""
    await _service(request).delete_session(session_id)


@router.get("/sessions/{session_id}/messages", response_model=MessageList)
async def list_messages(session_id: str, request: Request) -> MessageList:
    """拉取会话历史，用于刷新后恢复。"""
    session = await _service(request).get_session(session_id)
    return MessageList(items=[MessageItem(**item.to_dict()) for item in session.messages])


@router.get("/sessions/{session_id}/usage", response_model=UsageOut)
async def get_usage(session_id: str, request: Request) -> UsageOut:
    """会话累计 token 与成本，供前端展示用量。"""
    return UsageOut(**await _service(request).get_usage(session_id))


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
    # 允许前端跨源读取 stream id，用于断线续传
    "Access-Control-Expose-Headers": "X-Stream-ID",
}


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str, payload: MessageRequest, request: Request
) -> StreamingResponse:
    """发送消息，以 SSE 流式返回回答。

    生成在后台任务里跑，连接断开不会中断回答；/streams/{id} 可续传。
    """
    service = _service(request)
    # 先校验会话存在：一旦开始流式返回，HTTP 状态码就无法再改了
    state = await service.start_reply(session_id, payload.message, resolve_user_id(request))
    headers = dict(_SSE_HEADERS)
    headers["X-Stream-ID"] = state.stream_id
    return StreamingResponse(
        service.subscribe(state.stream_id, 0),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/streams/{stream_id}")
async def resume_stream(
    stream_id: str, request: Request, last_event_id: int = 0
) -> StreamingResponse:
    """断线续传：重放 last_event_id 之后的事件，并继续推送。"""
    header = request.headers.get("Last-Event-ID")
    if header and header.strip().isdigit():
        last_event_id = int(header.strip())
    service = _service(request)
    headers = dict(_SSE_HEADERS)
    headers["X-Stream-ID"] = stream_id
    return StreamingResponse(
        service.subscribe(stream_id, last_event_id),
        media_type="text/event-stream",
        headers=headers,
    )


@router.post("/streams/{stream_id}/cancel")
async def cancel_stream(stream_id: str, request: Request) -> dict[str, bool]:
    """用户主动停止：终止后台生成，不保存半截回答。"""
    return {"cancelled": _service(request).cancel_stream(stream_id)}
