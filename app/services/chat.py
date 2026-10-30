"""会话与消息服务，并把 agent 循环包装成 SSE 事件流。

契约见 SPEC 第二部分 2.2（发消息）与 2.4（会话与记忆）。
事件类型：token / tool_call / tool_result / end / error
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import MAX_STEPS, build_agent_graph
from app.agent.guard import TaintState, current_taint
from app.agent.model import build_chat_model
from app.agent.permissions import (
    DecisionChannel,
    LocalDecisionChannel,
    PermissionBroker,
    PermissionRequest,
    current_broker,
)
from app.agent.prompts import build_system_prompt, get_persona
from app.agent.resilience import (
    RetryStats,
    ToolBudget,
    current_retry_stats,
    current_tool_budget,
)
from app.core.config import Settings
from app.core.context import compose_messages, make_summary, plan_fold
from app.core.errors import ApiError
from app.core.tracing import new_trace_id, reset_trace, set_trace, trace_config
from app.core.usage import TokenUsage, estimate_usage, usage_from_output
from app.media.service import MediaService, current_media
from app.media.uploads import MAX_ATTACHMENTS, UploadStore, image_block, mime_of
from app.services.profile import ProfileService
from app.services.streams import CANCEL_POLL_SEC, EventLog, MemoryEventLog, StreamState
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
    "open_file": "system",
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
_TASK_ID_RE = re.compile(
    r"task_id[：:\s]*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
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
    return datetime.now(UTC).isoformat()


class ChatService:
    """会话与对话服务。持久化交给注入的 SessionStore，与后端实现无关。"""

    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        media: MediaService | None = None,
        profile: ProfileService | None = None,
        uploads: UploadStore | None = None,
        event_log: EventLog | None = None,
        decision_channel: DecisionChannel | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._media = media
        self._profile = profile
        self._uploads = uploads
        self._graph: Any = None
        self._mode: str = ""
        self._summarizer: Any = None
        self._graph_lock = asyncio.Lock()
        # 事件流与权限裁决都放在可替换的后端后面：单进程用内存，
        # 多进程用 Redis。默认给内存实现，测试和脚本不必配 Redis。
        self._log: EventLog = event_log if event_log is not None else MemoryEventLog()
        self._decisions: DecisionChannel = (
            decision_channel if decision_channel is not None else LocalDecisionChannel()
        )
        # 本进程内正在生成的流：取消时要能直接 cancel 掉任务，
        # 跨进程那一路只能靠 log.cancel() 写标记，见 cancel_stream
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        # 后台任务的强引用。asyncio 只对 task 持弱引用，不在这里存一份，
        # 任务可能在跑完前被 GC 回收（CPython 文档明确警告过这一点）
        self._background: set[asyncio.Task[Any]] = set()

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

    async def get_usage(self, session_id: str) -> dict[str, Any]:
        """会话累计用量。前端用来展示「本会话花了多少」。"""
        session = await self.get_session(session_id)
        return dict(session.usage)

    async def set_persona(self, session_id: str, persona: str) -> str:
        """切换对话风格，返回实际生效的 key。

        未知 key 不报错而是回落默认（见 ``get_persona``）：前端选项来自
        ``GET /personas``，正常不会传错；真传错了，让整轮对话 500 也没好处。

        切换只影响**后续轮次**的 system prompt，不改历史消息——
        已经聊过的内容按当时的风格生成，这是事实，不该被追溯修改。
        """
        session = await self.get_session(session_id)
        resolved = get_persona(persona)
        session.persona = resolved.key
        await self._store.save(session)
        return resolved.key

    def _image_blocks(self, attachments: list[str]) -> list[dict[str, Any]]:
        """把 upload_id 变成多模态内容块。取不到的附件跳过并记日志。

        跳过而不是报错：一张图在传输过程中被删掉（用户点了移除、或者磁盘被清理），
        不该让整轮对话失败——其余几张还能用，模型也会照常回答。
        """
        if not attachments or self._uploads is None:
            return []
        blocks: list[dict[str, Any]] = []
        for upload_id in attachments[:MAX_ATTACHMENTS]:
            item = self._uploads.get(upload_id)
            if item is None:
                logger.warning("附件不存在，已跳过 | id=%s", upload_id)
                continue
            path = self._uploads.path_of(item)
            if not path.is_file():
                logger.warning("附件文件已丢失，已跳过 | id=%s", upload_id)
                continue
            blocks.append(image_block(path, mime_of(item)))
        return blocks

    # ---------- 对话 ----------

    async def start_reply(
        self,
        session_id: str,
        text: str,
        user_id: str = "default",
        attachments: list[str] | None = None,
    ) -> StreamState:
        """创建事件流并**在后台**开始生成。

        生成任务不挂在 HTTP 请求上：客户端断开只会停止订阅，
        回答继续跑完并落库，之后可用 Last-Event-ID 续传。
        """
        await self.get_session(session_id)  # 先校验，避免流开始后才报错
        stream_id = uuid4().hex
        await self._log.create(stream_id, session_id)
        state = StreamState(stream_id=stream_id, session_id=session_id, log=self._log)
        task: asyncio.Task[Any] = asyncio.create_task(
            self._run_reply(state, session_id, text, user_id, attachments or [])
        )
        state.task = task
        self._tasks[stream_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(stream_id, None))
        return state

    async def stream_reply(
        self, session_id: str, text: str, user_id: str = "default"
    ) -> AsyncIterator[str]:
        """兼容旧调用：等价于 start_reply + subscribe(0)。"""
        state = await self.start_reply(session_id, text, user_id)
        async for frame in self.subscribe(state.stream_id, 0):
            yield frame

    async def cancel_stream(self, stream_id: str) -> bool:
        """用户主动停止：取消后台生成，且不保存半截回答。

        两步都要做，顺序不能反：

        1. 先在共享存储里写取消标记——跨进程部署时这个请求可能落在
           另一台机器上，那边没有本进程的 Task 可以 cancel，只能靠标记
           让生成端自己退出（见 ``_run_reply`` 里的 ``_cancelled_elsewhere``）；
        2. 再取消本地任务——同进程时立即生效，不必等一个轮询周期。
        """
        if not await self._log.cancel(stream_id):
            return False
        task = self._tasks.get(stream_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    async def subscribe(self, stream_id: str, last_event_id: int = 0) -> AsyncIterator[str]:
        """从 last_event_id 之后重放，并继续等待新事件。

        后端存储（Redis）抽风时必须**发一条 error 事件**再结束，不能让生成器
        直接抛出去：SSE 的响应头早就发出去了，异常只能让连接断掉，客户端拿到的是
        一个 200 + 空响应体——用户看到的是「点了发送，什么都没发生」，
        既没有回答也没有报错。实测就是这么撞上的：重启后第一次对话，
        Redis 冷连接超过 5 秒默认读超时，整条流静默消失。
        """
        try:
            if await self._log.meta(stream_id) is None:
                yield _sse(
                    {
                        "type": "error",
                        "code": "STREAM_NOT_FOUND",
                        "message": "事件流不存在或已过期，请刷新会话重新获取。",
                    }
                )
                return
            async for item in self._log.tail(stream_id, max(0, int(last_event_id or 0))):
                if item is None:
                    # 空闲心跳：公网反代会掐断长时间无数据的 SSE 连接
                    yield ": ping\n\n"
                    continue
                seq, payload = item
                yield f"id: {seq}\n{_sse(payload)}"
        except Exception as exc:  # 任何存储异常都要转成可读事件
            logger.warning("事件流中断 | stream=%s err=%r", stream_id, exc, exc_info=True)
            yield _sse(
                {
                    "type": "error",
                    "code": "STREAM_UNAVAILABLE",
                    "message": "事件流暂时不可用（存储连接异常），请重试或刷新会话。",
                }
            )

    async def _run_reply(
        self,
        state: StreamState,
        session_id: str,
        text: str,
        user_id: str,
        attachments: list[str] | None = None,
    ) -> None:
        # 一轮对话一个 trace_id：日志、SSE 事件、LangSmith trace 三处共用同一个值，
        # 出问题时用户报一个 id 就能定位到完整链路
        trace_id = new_trace_id()
        token = set_trace(trace_id)
        started = time.perf_counter()
        try:
            await self._generate(
                state, session_id, text, user_id, trace_id, started, attachments or []
            )
        except asyncio.CancelledError:
            state.cancelled = True
            logger.info("本轮被用户中止 | session=%s", session_id)
            raise
        finally:
            logger.info(
                "本轮结束 | session=%s user=%s 耗时=%.2fs cancelled=%s",
                session_id,
                user_id,
                time.perf_counter() - started,
                state.cancelled,
            )
            reset_trace(token)
            await state.finish()

    async def _generate(
        self,
        state: StreamState,
        session_id: str,
        text: str,
        user_id: str,
        trace_id: str = "",
        started: float = 0.0,
        attachments: list[str] | None = None,
    ) -> None:
        """把一轮问答写入事件流。

        ``started`` 是 ``stream()`` 里的 ``perf_counter()`` 起点。传进来而不是
        在这里重新计时，是为了让前端显示的耗时和日志里的「耗时=」对得上——
        两个数字不一致时，人第一反应是怀疑统计口径，而不是去查真正的慢在哪。
        """
        session = await self.get_session(session_id)

        # 预算闸放在最前面：超限的请求连用户消息都不落库，
        # 否则历史里会留下一句永远得不到回答的话
        if not self._within_budget(session):
            await state.emit(
                {
                    "type": "error",
                    "code": "BUDGET_EXCEEDED",
                    "message": (
                        f"本会话已累计 {session.usage.get('total_tokens', 0)} tokens，"
                        f"超过上限 {self._settings.max_tokens_per_session}，请新建会话继续。"
                    ),
                    "trace_id": trace_id,
                }
            )
            return

        if attachments and self._uploads is None:
            # 宁可整轮拒绝，也不要把图悄悄丢掉然后假装回答了一个看图问题
            await state.emit(
                {
                    "type": "error",
                    "code": "VISION_NOT_ENABLED",
                    "message": "当前未启用图片输入，请在 .env 中配置上传目录后重启后端。",
                    "trace_id": trace_id,
                }
            )
            return

        session.messages.append(
            ChatMessage(
                id=str(uuid4()),
                role="user",
                content=text,
                created_at=_now(),
                attachments=list(attachments or []),
            )
        )
        self._trim(session)
        await self._store.save(session)

        try:
            graph, mode = await self._ensure_graph()
        except Exception as exc:  # noqa: BLE001 - 配置或探测失败要如实报错
            await state.emit(
                {
                    "type": "error",
                    "code": "MODEL_UNAVAILABLE",
                    "message": str(exc),
                    "trace_id": trace_id,
                }
            )
            return

        # 长期偏好并入 system prompt：只带确认过的条目，避免每轮都膨胀
        # 提示词按本会话选定的风格组装：核心规则 + 领域规则（旅行类才有）+ 说话方式。
        # 「闲聊时别推行程」不能靠加一句规则——实测那只是概率改善，
        # 得让闲聊型人格根本拿不到旅行规则，见 app/agent/prompts.py 的说明。
        system_text = build_system_prompt(session.persona)
        if self._profile is not None:
            block = await self._profile.render_block(user_id)
            if block:
                system_text = f"{system_text}\n\n{block}"

        await self._refresh_summary(session, system_text)
        await self._store.save(session)
        history = [(item.role, item.content) for item in session.messages]
        messages = compose_messages(system_text, session.summary, history, session.summarized_count)

        # 图片只挂**当前轮**。每轮重发会让 token 随轮数线性膨胀（一张 1280 的图约 1.5k token），
        # 而用户接着追问的多半还是同一张图——上一轮的回答里已经有模型对它的描述。
        # 代价是「隔一轮再问图里的细节」会问不到，这是刻意选的：宁可让用户重发一次，
        # 也不要在第 10 轮还替第 1 轮的图付钱。
        blocks = self._image_blocks(attachments or [])
        if blocks:
            messages[-1] = HumanMessage(
                content=[{"type": "text", "text": text or "请看这张图。"}, *blocks]
            )

        # 图放在后台任务里跑，图事件与权限请求都送进同一个队列，生成器只从队列取。
        # 否则工具在等用户确认期间会阻塞 astream_events，整条流被卡死。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _notify_permission(permission: PermissionRequest) -> None:
            await queue.put(("permission", permission.to_event()))

        broker = PermissionBroker(self._decisions, _notify_permission)
        ctx_token = current_broker.set(broker)
        # 必须在创建后台任务之前设置：ContextVar 由任务继承，
        # 工具层要靠它拿到**同一个** MediaService 实例（任务存在实例里）
        media_token = current_media.set(self._media)
        # 同上：工具重试发生在后台任务里，统计要回到本轮
        retry_stats = RetryStats()
        retry_token = current_retry_stats.set(retry_stats)
        # 工具调用预算也按轮重置：模型对同一工具反复试参数时，
        # 光靠 prompt 约束不住，必须由代码兜底
        tool_budget = ToolBudget(
            per_tool=self._settings.tool_max_calls_per_tool,
            total=self._settings.tool_max_calls_total,
        )
        budget_token = current_tool_budget.set(tool_budget)
        # 污点状态同样按轮重置：本轮读到的内容里出现注入指令时，
        # 后续有副作用的工具会被拦下；用户重发一次明确指令即回到干净状态
        taint = TaintState()
        taint_token = current_taint.set(taint)

        async def _run_graph() -> None:
            try:
                async for graph_event in graph.astream_events(
                    {"messages": messages},
                    version="v2",
                    config={
                        "recursion_limit": MAX_STEPS * 2 + 5,
                        # run_name / metadata 会出现在 LangSmith 面板上，
                        # 便于按 session_id 或 trace_id 直接筛出这一轮
                        **trace_config(trace_id, session_id=session_id, user_id=user_id),
                    },
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
        # 本轮生图任务，随回答一起持久化，刷新后前端据此重建图片卡片
        media_task_ids: list[str] = []
        # 本轮工具调用次数，只用于日志里的可观测性指标
        tool_calls = 0
        # 本轮真实 token 用量，从模型的 usage_metadata 累加
        turn_usage = TokenUsage()
        # 上次检查「别的进程有没有取消我」的时刻。跨进程取消只能轮询共享标记，
        # 限流到每秒一次：每条 token 都查一次 Redis，延迟和成本都不划算
        last_cancel_check = 0.0

        async def _cancelled_elsewhere() -> bool:
            nonlocal last_cancel_check
            now = time.monotonic()
            if now - last_cancel_check < CANCEL_POLL_SEC:
                return False
            last_cancel_check = now
            if not await self._log.is_cancelled(state.stream_id):
                return False
            logger.info("检测到跨进程取消 | stream=%s", state.stream_id)
            state.cancelled = True
            return True

        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    # 采集/生图这类长工具调用期间没有 token；公网反代会把长时间
                    # 无数据的 SSE 连接掐断，这里定期发心跳保活（前端忽略该类型）
                    await state.emit({"type": "ping"})
                    if await _cancelled_elsewhere():
                        break
                    continue

                if await _cancelled_elsewhere():
                    break

                if kind == "permission":
                    await state.emit(payload)

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
                                await state.emit({"type": "token", "content": piece})

                    elif name == "on_tool_start":
                        tool_calls += 1
                        logger.info(
                            "调用工具 | tool=%s category=%s",
                            event.get("name") or "",
                            tool_category(event.get("name") or ""),
                        )
                        await state.emit(
                            {
                                "type": "tool_call",
                                "call_id": str(event.get("run_id") or ""),
                                "tool": event.get("name") or "",
                                "category": tool_category(event.get("name") or ""),
                                "args": event["data"].get("input") or {},
                            }
                        )

                    elif name == "on_tool_end":
                        output = event["data"].get("output")
                        summary = _summarize(output)
                        status = "error" if summary.startswith(_ERROR_PREFIXES) else "ok"
                        if event.get("name") == "generate_image" and status == "ok":
                            raw = _as_text(getattr(output, "content", output)) or str(output)
                            match = _TASK_ID_RE.search(raw)
                            if match and match.group(1) not in media_task_ids:
                                media_task_ids.append(match.group(1))
                        await state.emit(
                            {
                                "type": "tool_result",
                                "call_id": str(event.get("run_id") or ""),
                                "status": status,
                                "summary": summary,
                            }
                        )

                    elif name == "on_tool_error":
                        await state.emit(
                            {
                                "type": "tool_result",
                                "call_id": str(event.get("run_id") or ""),
                                "status": "error",
                                "summary": _truncate(
                                    str(event["data"].get("error") or "工具执行失败")
                                ),
                            }
                        )

                    elif name == "on_chat_model_end":
                        # native 模式下每轮工具调用都会多一次模型调用，
                        # 所以要在这里逐次累加，而不是只看最后一条消息
                        found = usage_from_output(event["data"].get("output"))
                        if found is not None:
                            turn_usage.add(*found)

                    elif name == "on_chain_end":
                        output = event["data"].get("output")
                        if isinstance(output, dict) and "messages" in output:
                            final_state = output
        finally:
            current_broker.reset(ctx_token)
            current_media.reset(media_token)
            current_retry_stats.reset(retry_token)
            current_tool_budget.reset(budget_token)
            current_taint.reset(taint_token)
            await broker.cancel_all()
            if not runner.done():
                runner.cancel()

        if state.cancelled:
            # 用户点了停止：哪怕模型刚好跑完，也不把这一轮写进历史。
            # 放在 failed/空回答判断之前——取消时 final_state 往往是 None，
            # 走后面的分支会先报一条 MODEL_EMPTY_RESPONSE，等于把「用户主动停止」
            # 说成「模型出错」
            return

        if failed is not None:
            await state.emit({"type": "error", **failed, "trace_id": trace_id})
            return

        answer, finish_reason = _extract_answer(final_state, streamed)

        if mode != "native" and answer:
            # structured 模式无法逐字流式，整段一次性给出
            await state.emit({"type": "token", "content": answer})

        if not answer.strip():
            await state.emit(
                {
                    "type": "error",
                    "code": "MODEL_EMPTY_RESPONSE",
                    "message": "模型没有返回可显示的文本。",
                    "trace_id": trace_id,
                }
            )
            return

        # 拿不到真实用量（部分本地模型不返回 usage）就退回估算，
        # 但必须把 estimated 标出来，不能让估算值冒充账单
        if turn_usage.calls == 0:
            est_in, est_out = estimate_usage(
                [system_text, *(content for _, content in history), text], answer
            )
            turn_usage.add(est_in, est_out, estimated=True)

        turn_usage_dict = turn_usage.to_dict(self._settings.model_name, self._settings.model_price)
        session_usage = TokenUsage.from_dict(session.usage)
        session_usage.merge(turn_usage)
        turns = int(session.usage.get("turns") or 0) + 1
        session.usage = session_usage.to_dict(self._settings.model_name, self._settings.model_price)
        session.usage["turns"] = turns

        message_id = str(uuid4())
        # 耗时在这里结算：回答已生成完，end 事件还没发，
        # 前端拿到的数字 = 用户从点发送到看见完整回答的等待时间
        latency_ms = int((time.perf_counter() - started) * 1000) if started else 0
        session.messages.append(
            ChatMessage(
                id=message_id,
                role="assistant",
                content=answer,
                created_at=_now(),
                media_task_ids=media_task_ids,
                usage=turn_usage_dict,
                latency_ms=latency_ms,
            )
        )
        self._trim(session)
        await self._store.save(session)

        # 回答已落库，后台抽偏好；失败不影响本轮，也不阻塞 SSE
        if self._profile is not None:
            self._spawn(
                self._profile.maybe_suggest(user_id, text, [*history, ("assistant", answer)])
            )

        logger.info(
            "本轮完成 | mode=%s 回答=%d 字 finish=%s 工具调用=%d 生图任务=%d "
            "tokens=%d/%d 成本=%s 重试=%d 估算=%s",
            mode,
            len(answer),
            finish_reason,
            tool_calls,
            len(media_task_ids),
            turn_usage.input_tokens,
            turn_usage.output_tokens,
            turn_usage_dict["cost"],
            retry_stats.retries,
            turn_usage.estimated,
        )

        await state.emit(
            {
                "type": "end",
                "message_id": message_id,
                "finish_reason": finish_reason,
                "trace_id": trace_id,
                "usage": {"turn": turn_usage_dict, "session": dict(session.usage)},
                "latency_ms": latency_ms,
                "tool_retries": retry_stats.to_dict(),
                "tool_budget": tool_budget.to_dict(),
                # 本轮是否读到过注入内容、拦了几次危险操作。前端不展示，
                # 但排查「为什么写文件没执行」时这是唯一线索
                "taint": taint.to_dict(),
            }
        )

    # ---------- 内部 ----------

    def _within_budget(self, session: ChatSession) -> bool:
        """会话累计 token 是否仍在预算内。0 表示不设上限。"""
        limit = self._settings.max_tokens_per_session
        if limit <= 0:
            return True
        return int(session.usage.get("total_tokens") or 0) < limit

    def _spawn(self, coro: Any) -> None:
        """起一个后台任务，并持有强引用直到它结束。

        fire-and-forget 的写法若不留引用，任务对象可能在中途被回收，
        表现为「偏好抽取偶尔不生效」这类难以复现的问题。
        """
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _ensure_graph(self) -> tuple[Any, str]:
        """首次调用时构图（含 tool calling 能力探测），之后复用。"""
        async with self._graph_lock:
            if self._graph is None:
                self._graph, self._mode = build_agent_graph(self._settings)
            return self._graph, self._mode

    async def _refresh_summary(self, session: ChatSession, system_prompt: str) -> None:
        """按预算决定是否把历史折叠进摘要。

        摘要失败时不推进 summarized_count，那批消息会继续留在窗口里——
        宁可多占一点上下文，也不能因为摘要失败而丢掉记忆。
        """
        history = [(item.role, item.content) for item in session.messages]
        plan = plan_fold(
            system_prompt=system_prompt,
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
            session.summary = await make_summary(self._summarizer, session.summary, plan.fold)
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
