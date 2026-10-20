"""会话与聊天路由（SSE），对齐 SPEC 契约 2.1 / 2.2。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.schemas.chat import MessageItem, MessageList, MessageRequest, SessionCreated
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


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str, payload: MessageRequest, request: Request
) -> StreamingResponse:
    """发送消息，以 SSE 流式返回回答。"""
    service = _service(request)
    # 先校验会话存在：一旦开始流式返回，HTTP 状态码就无法再改了
    await service.get_session(session_id)

    return StreamingResponse(
        service.stream_reply(session_id, payload.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
