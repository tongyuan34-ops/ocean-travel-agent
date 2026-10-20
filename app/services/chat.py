"""会话与消息服务，并把 agent 循环包装成 SSE 事件流。

契约见 SPEC 第二部分 2.2（发消息）与 2.4（会话与记忆）。
事件类型：token / tool_call / tool_result / end / error
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

from langchain_core.messages import AIMessage

from app.agent.graph import MAX_STEPS, SYSTEM_PROMPT, build_agent_graph
from app.agent.model import build_chat_model
from app.agent.permissions import PermissionBroker, PermissionRequest, current_broker
from app.core.config import Settings
from app.core.context import compose_messages, make_summary, plan_fold
from app.core.errors import ApiError
from app.media.service import MediaService, current_media
from app.storage import ChatMessage, ChatSession, SessionStore

logger = logging.getLogger(__name__)

# 每个会话保留最近多少轮对话（1 轮 = 一问一答）
MAX_TURNS = 20
# 工具名 → 契约中的 category
_TOOL_CATEGORY = {
    "get_weather": "info",
    "plan_route": "info",
    "search_poi": "info",
    "estimate_budget": "plan",
    "optimize_day_route": "plan",
    "recommend_stay": "plan",
    "recommend_transport": "plan",
    "plan_trip": "plan",
    "list_dir": "system",
    "read_file": "system",
    "write_file": "system",
    "create_dir": "system",
    "delete_path": "system",
    "set_wallpaper": "system",
    "search_web": "search",
    "fetch_url": "search",
    "parse_shared_link": "plugin",
    "search_social_session": "plugin",
    "generate_scraper": "plugin",
    "generate_image": "media",
    "list_image_templates": "media",
}
_SUMMARY_LIMIT = 200
# 工具返回文本以这些错误码开头时，tool_result 记为 error
_ERROR_PREFIXES = (
    "TOOL_NOT_ENABLED",
    "TOOL_FAILED",
    "PATH_NOT_ALLOWED",
    "PERMISSION_DENIED",
    "INVALID_REQUEST",
    "SEARCH_FAILED",
    "LINK_PARSE_FAILED",
    "FETCH_FAILED",
    "URL_NOT_ALLOWED",
    "MEDIA_BACKEND_MISSING",
    "MEDIA_UPSTREAM_ERROR",
    "MEDIA_UPSTREAM_TIMEOUT",
)


_OTHER_CATEGORY = "other"


def tool_category(tool_name: str) -> str:
    """工具名 → 契约里的 category。工具清单与 SSE 事件共用，避免两边不一致。"""
    return _TOOL_CATEGORY.get(tool_name, _OTHER_CATEGORY)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatService:
    """会话与对话服务。持久化交给注入的 SessionStore，与后端实现无关。"""

    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        media: MediaService | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._media = media
        self._graph: Any = None
        self._mode: str = ""
        self._summarizer: Any = None
        self._graph_lock = asyncio.Lock()

    # ---------- 会话管理 ----------

    async def create_session(self) -> ChatSession:
        session = ChatSession(id=str(uuid4()), created_at=_now())
        await self._store.create(session)
        return session

    async def get_session(self, session_id: str) -> ChatSession:
        session = await self._store.get(session_id)
        if session is None:
            raise ApiError("SESSION_NOT_FOUND", f"会话不存在：{session_id}")
        return session

    async def delete_session(self, session_id: str) -> None:
        await self._store.delete(session_id)

    # ---------- 对话 ----------

    async def stream_reply(
        self, session_id: str, text: str
    ) -> AsyncIterator[str]:
        """把一轮问答以 SSE 帧的形式产出。"""
        session = await self.get_session(session_id)
        session.messages.append(
            ChatMessage(id=str(uuid4()), role="user", content=text, created_at=_now())
        )
        self._trim(session)
        await self._store.save(session)

        try:
            graph, mode = await self._ensure_graph()
        except Exception as exc:  # noqa: BLE001 - 配置或探测失败要如实报错
            yield _sse({"type": "error", "code": "MODEL_UNAVAILABLE", "message": str(exc)})
            return

        await self._refresh_summary(session)
        await self._store.save(session)
        history = [(item.role, item.content) for item in session.messages]
        messages = compose_messages(
            SYSTEM_PROMPT, session.summary, history, session.summarized_count
        )

        # 图放在后台任务里跑，图事件与权限请求都送进同一个队列，生成器只从队列取。
        # 否则工具在等用户确认期间会阻塞 astream_events，整条流被卡死。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _notify_permission(permission: PermissionRequest) -> None:
            await queue.put(("permission", permission.to_event()))

        broker = PermissionBroker(_notify_permission)
        ctx_token = current_broker.set(broker)
        # 必须在创建后台任务之前设置：ContextVar 由任务继承，
        # 工具层要靠它拿到**同一个** MediaService 实例（任务存在实例里）
        media_token = current_media.set(self._media)

        async def _run_graph() -> None:
            try:
                async for graph_event in graph.astream_events(
                    {"messages": messages},
                    version="v2",
                    config={"recursion_limit": MAX_STEPS * 2 + 5},
                ):
                    await queue.put(("graph", graph_event))
            except Exception as exc:  # noqa: BLE001 - 交给消费端统一映射错误码
                await queue.put(("failed", exc))
                return
            await queue.put(("finished", None))

        runner = asyncio.create_task(_run_graph())

        streamed = ""
        final_state: dict | None = None
        failed: dict | None = None

        try:
            while True:
                kind, payload = await queue.get()

                if kind == "permission":
                    yield _sse(payload)

                elif kind == "failed":
                    failed = _map_failure(payload)
                    break

                elif kind == "finished":
                    break

                else:
                    event = payload
                    name = event.get("event")

                    if name == "on_chat_model_stream":
                        chunk = event["data"].get("chunk")
                        # 兜底取 chunk 本身：万一上游给的是裸字符串，
                        # getattr(..., "content", None) 会得到 None 并**静默丢掉**
                        # 这一段输出——用户会看到回答凭空少一截，且没有任何报错
                        piece = _as_text(getattr(chunk, "content", chunk))
                        if piece:
                            streamed += piece
                            # structured 模式内部会输出 JSON 决策，不能直接透给用户
                            if mode == "native":
                                yield _sse({"type": "token", "content": piece})

                    elif name == "on_tool_start":
                        yield _sse(
                            {
                                "type": "tool_call",
                                "call_id": str(event.get("run_id") or ""),
                                "tool": event.get("name") or "",
                                "category": tool_category(event.get("name") or ""),
                                "args": event["data"].get("input") or {},
                            }
                        )

                    elif name == "on_tool_end":
                        summary = _summarize(event["data"].get("output"))
                        status = (
                            "error" if summary.startswith(_ERROR_PREFIXES) else "ok"
                        )
                        yield _sse(
                            {
                                "type": "tool_result",
                                "call_id": str(event.get("run_id") or ""),
                                "status": status,
                                "summary": summary,
                            }
                        )

                    elif name == "on_tool_error":
                        yield _sse(
                            {
                                "type": "tool_result",
                                "call_id": str(event.get("run_id") or ""),
                                "status": "error",
                                "summary": _truncate(
                                    str(event["data"].get("error") or "工具执行失败")
                                ),
                            }
                        )

                    elif name == "on_chain_end":
                        output = event["data"].get("output")
                        if isinstance(output, dict) and "messages" in output:
                            final_state = output
        finally:
            current_broker.reset(ctx_token)
            current_media.reset(media_token)
            broker.cancel_all()
            if not runner.done():
                runner.cancel()

        if failed is not None:
            yield _sse({"type": "error", **failed})
            return

        answer, finish_reason = _extract_answer(final_state, streamed)

        if mode != "native" and answer:
            # structured 模式无法逐字流式，整段一次性给出
            yield _sse({"type": "token", "content": answer})

        if not answer.strip():
            yield _sse(
                {
                    "type": "error",
                    "code": "MODEL_EMPTY_RESPONSE",
                    "message": "模型没有返回可显示的文本。",
                }
            )
            return

        message_id = str(uuid4())
        session.messages.append(
            ChatMessage(id=message_id, role="assistant", content=answer, created_at=_now())
        )
        self._trim(session)
        await self._store.save(session)

        yield _sse({"type": "end", "message_id": message_id, "finish_reason": finish_reason})

    # ---------- 内部 ----------

    async def _ensure_graph(self) -> tuple[Any, str]:
        """首次调用时构图（含 tool calling 能力探测），之后复用。"""
        async with self._graph_lock:
            if self._graph is None:
                self._graph, self._mode = build_agent_graph(self._settings)
            return self._graph, self._mode

    async def _refresh_summary(self, session: ChatSession) -> None:
        """按预算决定是否把历史折叠进摘要。

        摘要失败时不推进 summarized_count，那批消息会继续留在窗口里——
        宁可多占一点上下文，也不能因为摘要失败而丢掉记忆。
        """
        history = [(item.role, item.content) for item in session.messages]
        plan = plan_fold(
            system_prompt=SYSTEM_PROMPT,
            summary=session.summary,
            history=history,
            summarized_count=session.summarized_count,
            max_tokens=self._settings.max_context_tokens,
        )
        if not plan.needed:
            return

        if self._summarizer is None:
            self._summarizer = build_chat_model(self._settings)
        try:
            session.summary = await make_summary(
                self._summarizer, session.summary, plan.fold
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败不能影响正常回答
            logger.warning("上下文摘要生成失败，本轮沿用原摘要：%s", exc)
            return
        session.summarized_count = plan.new_summarized_count

    @staticmethod
    def _trim(session: ChatSession) -> None:
        """裁剪超长会话。

        裁掉的消息若已被折叠进摘要，需同步回退计数，保持下标一致。
        """
        limit = MAX_TURNS * 2
        excess = len(session.messages) - limit
        if excess > 0:
            del session.messages[:excess]
            session.summarized_count = max(0, session.summarized_count - excess)


# ---------- 模块级辅助函数 ----------


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    return ""


def _truncate(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize(output: Any) -> str:
    content = getattr(output, "content", output)
    return _truncate(_as_text(content) or str(content))


def _extract_answer(final_state: dict | None, streamed: str) -> tuple[str, str]:
    """取最终回答。

    structured 模式下流式内容是内部 JSON 决策，不能直接用，
    因此优先从最终状态里取最后一条「不含 tool_calls 的 AI 消息」。
    """
    if final_state:
        for message in reversed(final_state.get("messages") or []):
            if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
                text = _as_text(message.content)
                if text.strip():
                    reason = ""
                    meta = getattr(message, "response_metadata", None) or {}
                    if meta.get("finish_reason") == "length":
                        reason = "length"
                    return text, reason or "stop"
    return streamed, "stop"


def _map_failure(exc: Exception) -> dict[str, str]:
    """把异常映射成契约错误码。"""
    name = type(exc).__name__
    text = str(exc)
    if "Timeout" in name or "timeout" in text.lower():
        return {"code": "MODEL_TIMEOUT", "message": f"模型响应超时：{text}"}
    if "Connection" in name or "Connect" in name:
        return {"code": "MODEL_UNAVAILABLE", "message": f"无法连接模型服务：{text}"}
    return {"code": "INTERNAL_ERROR", "message": f"处理失败：{text}"}
