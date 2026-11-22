"""会话与消息服务，并把 agent 循环包装成 SSE 事件流。

契约见 SPEC 第二部分 2.2（发消息）与 2.4（会话与记忆）。
事件类型：token / tool_call / tool_result / end / error
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage

from app.agent.graph import build_agent_graph
from app.agent.guard import TaintState, current_taint
from app.agent.permissions import (
    DecisionChannel,
    LocalDecisionChannel,
    PermissionBroker,
    PermissionRequest,
    current_broker,
)
from app.agent.resilience import (
    RetryStats,
    ToolBudget,
    current_retry_stats,
    current_tool_budget,
)
from app.agent.toolset import bind_active, route, tools_for
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.metrics import metrics
from app.core.tracing import new_trace_id, reset_trace, set_trace
from app.core.usage import TokenUsage, estimate_usage
from app.mcp.registry import all_tools
from app.media.service import MediaService, current_media, current_ref_images
from app.media.uploads import UploadStore
from app.services.profile import ProfileService
from app.services.repair import Diagnosis, RepairPolicy, build_repair_policy
from app.services.selfcheck import check_turn
from app.services.sessions import SessionService, now_iso
from app.services.streams import EventLog, MemoryEventLog, StreamState
from app.services.turn_context import TurnContext
from app.services.turn_events import extract_answer, sse
from app.services.turn_runtime import TurnRun, run_attempt
from app.skills import (
    bind_active_skills,
    get_skill,
    match_skills,
    split_command,
    tools_for_skills,
)
from app.storage import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

# 一轮最多跑几次模型。1 次正常 + 1 次自愈重试，再多就是在拿用户的钱试错：
# 同一句纠正话术重复第三遍，模型大概率还是同样的行为
MAX_REPAIR_ATTEMPTS = 2
# 空回答自动重试次数。上游偶发「HTTP 200 + content 为空」，原样重跑一次基本就正常；
# 只重试一次——第二次还是空，说明不是抖动，继续跑只会让用户白等、白花钱
MAX_EMPTY_RETRIES = 1
# 进程内最多缓存多少张 agent 图（按本轮工具集合区分）。换话题才会新建，
# 4 张足够覆盖「行程 / 文档 / 文件 / 闲聊」这几种来回切换，再多就是长尾泄漏
_MAX_GRAPHS = 4
# 一轮最多额外绑多少个技能工具。技能是用户可扩展的，不设上限的话，
# 一个装了很多技能的目录会把每轮的工具 schema 重新撑爆
MAX_SKILL_TOOLS = 4


@dataclass
class _TurnSetup:
    """准备阶段产出、跑图与收尾都要用的东西。"""

    session: ChatSession
    ctx: TurnRun
    broker: PermissionBroker
    # (ContextVar, token)：由 _generate 的 finally 统一复位
    tokens: list[tuple[Any, Any]]
    model_text: str
    explicit_skill: str
    active_box: set[str]
    skill_box: set[str]
    system_text: str
    history: list[tuple[str, str]]
    messages: list[Any]
    tool_budget: ToolBudget
    taint: TaintState
    retry_stats: RetryStats
    mode: str


@dataclass
class _TurnOutcome:
    """跑图的结果。默认值让「什么都没发生」也是合法状态。"""

    answer: str = ""
    finish_reason: str = ""
    failed: dict[str, Any] | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    media_task_ids: list[str] = field(default_factory=list)
    tool_calls: int = 0
    called_tools: set[str] = field(default_factory=set)


class ChatService:
    """一轮对话的编排：流生命周期、agent 循环、事件与用量结算。

    会话的增删查交给 ``SessionService``（app/services/sessions.py），
    这里只在编排中读取/保存它们。
    """

    def __init__(
        self,
        settings: Settings,
        sessions: SessionService,
        media: MediaService | None = None,
        profile: ProfileService | None = None,
        uploads: UploadStore | None = None,
        event_log: EventLog | None = None,
        decision_channel: DecisionChannel | None = None,
        repair: RepairPolicy | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._media = media
        self._profile = profile
        self._uploads = uploads
        # 上下文装配单独成类：提示词、历史、附件、摘要折叠都在里面，
        # 这里只负责在编排时调用它
        self._context = TurnContext(settings, uploads=uploads, profile=profile, media=media)
        # 自愈策略是注入的：主流程不认识具体规则，测试可以换成
        # 「永远说不用修」的空策略，从而把重试路径单独关掉
        self._repair: RepairPolicy = repair if repair is not None else build_repair_policy()
        # 图按「本轮绑定的工具集合」缓存。工具是按轮路由的（app/agent/toolset.py），
        # 不同轮次的工具子集不同，而 ``bind_tools`` 是构图时固定的——所以不能只存一张图。
        # key 用工具名的 frozenset：同话题的多轮会命中同一张，重新构图只在换话题时发生。
        self._graphs: dict[frozenset[str], Any] = {}
        self._mode: str = ""
        self._graph_lock = asyncio.Lock()
        # 正在生成中的会话：session_id → stream_id。
        #
        # 没有这个表时，同一会话的两轮会并发跑：两边各自 ``get`` 一份会话副本，
        # 各自 append 一条用户消息，各自 ``save``——后写的覆盖先写的，
        # 其中一条消息和它的回答就这么没了。用户看到的是「我刚发的那条不见了」，
        # 而日志里两轮都成功。
        #
        # 选择「拒绝」而不是「排队」：同一会话上并发的两轮几乎一定是误触
        # （连点发送、两个标签页），排队会让用户等一份他并不想要的结果。
        self._busy_sessions: dict[str, str] = {}
        # 租户级的在跑轮数：只用来做配额判断，真正的占用状态仍在 _busy_sessions。
        # 单独记账而不是每次去数会话，是因为统计要经过存储，而它在别的进程里
        self._busy_tenants: dict[str, int] = {}
        # stream_id → tenant。释放时靠它把租户计数减回去，
        # 否则取消/异常那条路径会漏减，配额就永远还不上了
        self._stream_tenant: dict[str, str] = {}
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

    def attachment_meta(
        self, ids: list[str], user_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """历史消息的附件元数据。实现在 TurnContext，路由只认这一个入口。"""
        return self._context.attachment_meta(ids, user_id)

    # ---------- 对话 ----------

    async def start_reply(
        self,
        session_id: str,
        text: str,
        user_id: str = "default",
        attachments: list[str] | None = None,
        *,
        tenant: str | None = None,
    ) -> StreamState:
        """创建事件流并**在后台**开始生成。

        生成任务不挂在 HTTP 请求上：客户端断开只会停止订阅，
        回答继续跑完并落库，之后可用 Last-Event-ID 续传。
        """
        # 先校验归属：越权的会话必须在流开始之前就被挡住，
        # 一旦开始流式返回，HTTP 状态码就无法再改了
        session = await self._sessions.get(session_id, tenant)
        tenant_key = session.tenant

        # 并发闸门。三处都要卡：同一个会话上重复发（会丢消息）、
        # 整体并发过多、以及单个租户吃光全局额度（每一轮都是真金白银的模型调用）。
        # 放在 ``create_task`` 之前，这样被拒的请求连后台任务都不会建。
        if session_id in self._busy_sessions:
            raise ApiError("RATE_LIMITED", "这个会话上一条回答还在生成，等它结束或先点停止")
        limit = self._settings.max_concurrent_replies
        if limit > 0 and len(self._busy_sessions) >= limit:
            raise ApiError(
                "RATE_LIMITED",
                f"同时进行的对话已达上限 {limit} 轮，等其中一轮结束再发",
            )
        # 租户级配额单独算：全局上限是「整个服务最多 N 轮」，一个租户开 N 个会话
        # 就能把它吃光，其他租户全部 429。这不是资源问题，是可用性问题
        per_tenant = self._settings.max_concurrent_replies_per_tenant
        if per_tenant > 0 and self._busy_tenants.get(tenant_key, 0) >= per_tenant:
            raise ApiError(
                "RATE_LIMITED",
                f"该租户同时进行的对话已达上限 {per_tenant} 轮，等其中一轮结束再发",
            )

        stream_id = uuid4().hex
        await self._log.create(stream_id, session_id)
        state = StreamState(stream_id=stream_id, session_id=session_id, log=self._log)
        task: asyncio.Task[Any] = asyncio.create_task(
            self._run_reply(state, session_id, text, user_id, attachments or [])
        )
        state.task = task
        self._tasks[stream_id] = task
        self._busy_sessions[session_id] = stream_id
        self._busy_tenants[tenant_key] = self._busy_tenants.get(tenant_key, 0) + 1
        self._stream_tenant[stream_id] = tenant_key

        def _release(_: asyncio.Task[Any]) -> None:
            # 正常收尾时 ``_run_reply`` 已经放过了，这里是兜底：
            # 任务在建起来之前就被取消、或者异常发生在 finally 之外时，
            # 只剩这一条路径能清掉，否则那个会话永远发不出新消息。
            self._tasks.pop(stream_id, None)
            self._release_slot(session_id, stream_id)

        task.add_done_callback(_release)
        return state

    async def stream_reply(
        self,
        session_id: str,
        text: str,
        user_id: str = "default",
        attachments: list[str] | None = None,
    ) -> AsyncIterator[str]:
        """兼容旧调用：等价于 start_reply + subscribe(0)。"""
        state = await self.start_reply(session_id, text, user_id, attachments)
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
                yield sse(
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
                yield f"id: {seq}\n{sse(payload)}"
        except Exception as exc:  # 任何存储异常都要转成可读事件
            logger.warning("事件流中断 | stream=%s err=%r", stream_id, exc, exc_info=True)
            yield sse(
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
            # 先放闸再收流：``finish()`` 一关，客户端立刻就能发下一条，
            # 而 done_callback 要等这一帧事件循环跑完才执行——中间那个空隙里
            # 用户看到的是「刚答完就发不出去」。
            self._release_slot(session_id, state.stream_id)
            await state.finish()

    def _release_slot(self, session_id: str, stream_id: str) -> None:
        """释放会话与租户的并发闸门。重复调用安全，正常收尾和 done_callback 都调。"""
        if self._busy_sessions.get(session_id) == stream_id:
            self._busy_sessions.pop(session_id, None)
        tenant = self._stream_tenant.pop(stream_id, None)
        if tenant is not None:
            left = self._busy_tenants.get(tenant, 0) - 1
            if left > 0:
                self._busy_tenants[tenant] = left
            else:
                self._busy_tenants.pop(tenant, None)

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

        三段式：准备（校验/路由/装配）、跑图（含空回答重试与自愈重跑）、
        收尾（用量、指标、落库、end 事件）。

        ``started`` 是 ``stream()`` 里的 ``perf_counter()`` 起点。传进来而不是
        在这里重新计时，是为了让前端显示的耗时和日志里的「耗时=」对得上——
        两个数字不一致时，人第一反应是怀疑统计口径，而不是去查真正的慢在哪。

        ContextVar 在准备阶段设置，必须由**这一层**统一复位：放进任何一段里，
        异常路径都会漏掉 reset，下一轮就会继承上一轮的工具预算和污点状态。
        """
        setup = await self._prepare_turn(
            state, session_id, text, user_id, trace_id, attachments or []
        )
        if setup is None:
            # 准备阶段已经发过 error 事件（预算/视觉/构图失败）
            return
        try:
            outcome = await self._converse(setup)
        finally:
            for var, token in setup.tokens:
                var.reset(token)
            await setup.broker.cancel_all()

        await self._finalize(setup, outcome, trace_id, started)

    async def _prepare_turn(
        self,
        state: StreamState,
        session_id: str,
        text: str,
        user_id: str,
        trace_id: str,
        attachments: list[str],
    ) -> _TurnSetup | None:
        """校验、落用户消息、路由工具、装配提示词，并设置本轮 ContextVar。

        返回 ``None`` 表示这一轮不该继续（已通过 SSE 告诉用户原因）。
        """
        session = await self._sessions.get(session_id)

        # 预算闸放在最前面：超限的请求连用户消息都不落库，
        # 否则历史里会留下一句永远得不到回答的话
        if not self._within_budget(session):
            await state.emit(
                {
                    "type": "error",
                    "code": "BUDGET_EXCEEDED",
                    "message": (
                        f"本会话已累计 {session.usage.get('total_tokens', 0)} tokens，"
                        f"超过上限 {self._settings.max_tokens_per_session}"
                    ),
                    "trace_id": trace_id,
                }
            )
            return None

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
            return None

        # 技能命令（``/skill <key>`` 或直接 ``/<key>``）只影响这一轮：
        # 命令本身不是对话内容，落库的是去掉命令后的正文，否则下一轮模型
        # 会在历史里看到一句没人回应的斜杠命令。
        explicit_skill, remainder = split_command(text)
        if explicit_skill:
            skill_item = get_skill(explicit_skill)
            name = skill_item.name if skill_item else explicit_skill
            model_text = remainder or f"使用「{name}」技能。"
            # 只统计用户主动输入命令的这一种。选择器固定下来的技能每轮都会生效，
            # 记在这里会把「一次选择」算成「每轮都点了一次」，指标失去意义
            metrics.inc("skill_activations_total", skill=explicit_skill, source="command")
        else:
            # 会话上固定的技能（前端选择器）在没有命令时生效
            sticky = session.skill if get_skill(session.skill) else ""
            explicit_skill = sticky
            model_text = text

        session.messages.append(
            ChatMessage(
                id=str(uuid4()),
                role="user",
                content=model_text,
                created_at=now_iso(),
                attachments=list(attachments),
            )
        )
        self._context.trim(session)
        await self._sessions.save(session)

        # 本轮绑哪些工具：按这句话（加附件类型）路由到能力组，只把相关的那几组
        # 交给模型。全量绑定要发 13k token 的工具 schema，一句「你好」也照付，
        # 而它一个都不会调——详见 app/agent/toolset.py 里的实测数字。
        groups = route(
            model_text,
            persona=session.persona,
            attachments=self._context.attachment_names(attachments),
            active=session.active_groups,
        )
        # 放一个可变集合进上下文：discover_tools 会往里加组，
        # 流结束后回写会话，下一轮就真的能用上
        active_box = bind_active(groups)

        # 本轮用哪些技能：显式选择优先；否则按触发词挑；一个都没命中时沿用
        # 上一轮的技能，让「那第二天呢」这种追问不至于掉回通用回答
        if explicit_skill:
            skill_keys = [explicit_skill]
        else:
            matched = match_skills(model_text)
            skill_keys = matched or [key for key in session.active_skills if get_skill(key)]
        # 同理放一个可变集合：use_skill 在工具里激活的技能，流结束后回写
        skill_box = bind_active_skills(skill_keys)

        tools = tools_for(groups, all_tools(), routing=self._settings.tool_routing)
        if skill_keys:
            known = {tool.name for tool in tools}
            extra = [tool for tool in tools_for_skills(skill_keys) if tool.name not in known]
            tools = [*tools, *extra[:MAX_SKILL_TOOLS]]
        # 这条日志是排查「模型为什么不调工具」的第一现场：工具没绑上时，
        # 它只会说「我做不到」，而看日志能一眼分清是路由没命中还是模型没用
        logger.info(
            "本轮工具 | 组=%s 技能=%s 工具=%d 模式=%s",
            ",".join(groups) or "无",
            ",".join(skill_keys) or "无",
            len(tools),
            self._settings.tool_routing,
        )
        # 这条日志是排查路由问题唯一的入口：用户报「模型说没有这个工具」时，
        # 先看这一行就知道是没命中组，还是命中了却没绑上
        logger.info(
            "工具路由 | 组=%s 工具=%d 个：%s",
            ",".join(groups) or "无",
            len(tools),
            ",".join(tool.name for tool in tools) or "无",
        )

        try:
            graph, mode = await self._graph_for(tools)
        except Exception as exc:  # noqa: BLE001 - 配置或探测失败要如实报错
            await state.emit(
                {
                    "type": "error",
                    "code": "MODEL_UNAVAILABLE",
                    "message": str(exc),
                    "trace_id": trace_id,
                }
            )
            return None

        # 提示词、历史、附件、事实、摘要折叠都在 TurnContext 里装配，
        # 这里只拿结果。摘要可能在这一步推进，所以要单独存一次
        prompt = await self._context.build(
            session,
            user_id=user_id,
            model_text=model_text,
            attachments=attachments,
            explicit_skill=explicit_skill,
        )
        await self._sessions.save(session)

        # 图放在后台任务里跑，图事件与权限请求都送进同一个队列，生成器只从队列取。
        # 否则工具在等用户确认期间会阻塞 astream_events，整条流被卡死。
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _notify_permission(permission: PermissionRequest) -> None:
            await queue.put(("permission", permission.to_event()))

        broker = PermissionBroker(self._decisions, _notify_permission)
        # 每设置一个 ContextVar 就记下 (变量, token)，由 _generate 的 finally 统一复位
        tokens: list[tuple[Any, Any]] = [
            (current_broker, current_broker.set(broker)),
            # 必须在创建后台任务之前设置：ContextVar 由任务继承，
            # 工具层要靠它拿到**同一个** MediaService 实例（任务存在实例里）
            (current_media, current_media.set(self._media)),
            # 同理：生图工具要拿本轮上传的原图当参考图，也靠任务继承
            (current_ref_images, current_ref_images.set(prompt.refs)),
        ]
        # 工具重试统计发生在后台任务里，要回到本轮
        retry_stats = RetryStats()
        tokens.append((current_retry_stats, current_retry_stats.set(retry_stats)))
        # 工具调用预算也按轮重置：模型对同一工具反复试参数时，
        # 光靠 prompt 约束不住，必须由代码兜底
        tool_budget = ToolBudget(
            per_tool=self._settings.tool_max_calls_per_tool,
            total=self._settings.tool_max_calls_total,
        )
        tokens.append((current_tool_budget, current_tool_budget.set(tool_budget)))
        # 污点状态同样按轮重置：本轮读到的内容里出现注入指令时，
        # 后续有副作用的工具会被拦下；用户重发一次明确指令即回到干净状态
        taint = TaintState()
        tokens.append((current_taint, current_taint.set(taint)))

        turn_usage = TokenUsage()
        # 一次尝试需要的、多趟之间不变的东西。打包传下去，
        # 免得 run_attempt 长出一排位置参数，调用处全靠顺序记忆
        ctx = TurnRun(
            state=state,
            graph=graph,
            mode=mode,
            queue=queue,
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            turn_usage=turn_usage,
            log=self._log,
        )
        return _TurnSetup(
            session=session,
            ctx=ctx,
            broker=broker,
            tokens=tokens,
            model_text=model_text,
            explicit_skill=explicit_skill,
            active_box=active_box,
            skill_box=skill_box,
            system_text=prompt.system_text,
            history=prompt.history,
            messages=prompt.messages,
            tool_budget=tool_budget,
            taint=taint,
            retry_stats=retry_stats,
            mode=mode,
        )

    async def _converse(self, setup: _TurnSetup) -> _TurnOutcome:
        """跑图：空回答重试一次；自检不通过且策略认领时，带纠正话术重跑一次。"""
        ctx = setup.ctx
        state = ctx.state
        turn_usage = ctx.turn_usage
        messages = setup.messages
        outcome = _TurnOutcome()

        streamed = ""
        final_state: dict | None = None
        failed: dict | None = None
        answer, finish_reason = "", ""
        # 本轮生图任务，随回答一起持久化，刷新后前端据此重建图片卡片
        media_task_ids: list[str] = []
        # 本轮工具调用次数，只用于日志里的可观测性指标
        tool_calls = 0
        # 本轮真正调用过的工具名。用来做收尾自检：
        # 模型声称「已提交」但这里没有 generate_image，就是假的
        called_tools: set[str] = set()
        warnings: list[dict[str, str]] = []
        repairs = 0
        empty_retries = 0

        while True:
            attempt = await run_attempt(ctx, messages)
            streamed = attempt.streamed
            final_state = attempt.final_state
            failed = attempt.failed
            media_task_ids.extend(attempt.media_task_ids)
            tool_calls += attempt.tool_calls
            called_tools |= attempt.called_tools

            if state.cancelled or failed is not None:
                break

            answer, finish_reason = extract_answer(final_state, streamed)
            if not answer.strip():
                # 空回答先当上游抖动处理：偶发 200 + 空 content 重跑一次就好。
                # finish_reason=length 例外——同一段 prompt 重跑还会被截断，
                # 只是让用户多等一次、多花一次钱
                if finish_reason != "length" and empty_retries < MAX_EMPTY_RETRIES:
                    empty_retries += 1
                    logger.warning(
                        "空回答，自动重试 | finish=%s 模式=%s 工具=%s 第 %d 次 trace=%s",
                        finish_reason or "无",
                        setup.mode,
                        ",".join(sorted(called_tools)) or "无",
                        empty_retries + 1,
                        ctx.trace_id,
                    )
                    await state.emit(
                        {
                            "type": "retry",
                            "reason": "EMPTY_RESPONSE",
                            "attempt": empty_retries + 1,
                            "message": "模型这一轮没有返回内容，正在自动重试一次。",
                        }
                    )
                    continue
                # 放弃前的最后一条证据：没有它，「空回答」只能靠猜是哪种
                logger.warning(
                    "空回答 | finish=%s 模式=%s 工具=%s 尝试=%d 用量=%d/%d 调用=%d trace=%s",
                    finish_reason or "无",
                    setup.mode,
                    ",".join(sorted(called_tools)) or "无",
                    empty_retries + 1,
                    turn_usage.input_tokens,
                    turn_usage.output_tokens,
                    turn_usage.calls,
                    ctx.trace_id,
                )
                break

            # 收尾自检：模型可能声称「已提交」但一次工具都没调。
            # 这一轮必须自己承认，不能等用户对着空气猜
            warnings = check_turn(
                user_text=setup.model_text,
                answer=answer,
                tool_names=called_tools,
                media_task_ids=media_task_ids,
            )
            if not warnings or repairs >= MAX_REPAIR_ATTEMPTS - 1:
                break

            instruction = self._repair.instruction(
                Diagnosis(
                    code=warnings[0]["code"],
                    title=warnings[0]["title"],
                    evidence=warnings[0].get("evidence", ""),
                    user_text=setup.model_text,
                    answer=answer,
                )
            )
            if not instruction:
                # 策略认领不了：照旧把问题摆给用户，不硬修
                break

            repairs += 1
            logger.warning(
                "本轮自愈重跑 | code=%s 第 %d 次尝试 | 工具=%s 生图任务=%d",
                warnings[0]["code"],
                repairs + 1,
                ",".join(sorted(called_tools)) or "无",
                len(media_task_ids),
            )
            await state.emit(
                {
                    "type": "retry",
                    "reason": warnings[0]["code"],
                    "attempt": repairs + 1,
                    "message": "模型上一轮只写了文字、没有真的调用工具，正在自动重试一次。",
                }
            )
            # 纠正话术只活在这一轮的内存里，不落库：
            # 用户的历史里不该出现一条「你刚才做错了」的系统消息
            messages = [*messages, HumanMessage(content=instruction)]
            # 上一轮的诊断作废，重跑后重新判
            warnings = []

        outcome.answer = answer
        outcome.finish_reason = finish_reason
        outcome.failed = failed
        outcome.warnings = warnings
        outcome.media_task_ids = media_task_ids
        outcome.tool_calls = tool_calls
        outcome.called_tools = called_tools
        return outcome

    async def _finalize(
        self,
        setup: _TurnSetup,
        outcome: _TurnOutcome,
        trace_id: str,
        started: float,
    ) -> None:
        """收尾：错误分支、用量与指标、助手消息落库、end 事件。"""
        session = setup.session
        state = setup.ctx.state
        turn_usage = setup.ctx.turn_usage
        answer = outcome.answer
        warnings = outcome.warnings
        media_task_ids = outcome.media_task_ids
        called_tools = outcome.called_tools

        if state.cancelled:
            # 用户点了停止：哪怕模型刚好跑完，也不把这一轮写进历史。
            # 放在 failed/空回答判断之前——取消时 final_state 往往是 None，
            # 走后面的分支会先报一条 MODEL_EMPTY_RESPONSE，等于把「用户主动停止」
            # 说成「模型出错」
            return

        if outcome.failed is not None:
            await state.emit({"type": "error", **outcome.failed, "trace_id": trace_id})
            return

        if setup.mode != "native" and answer:
            # structured 模式无法逐字流式，整段一次性给出
            await state.emit({"type": "token", "content": answer})

        if not answer.strip():
            # 「被长度上限截断」和「上游什么都没给」用户要做的事不一样：
            # 前者缩短问题就能解决，后者重试才有意义
            truncated = outcome.finish_reason == "length"
            await state.emit(
                {
                    "type": "error",
                    "code": "MODEL_TRUNCATED" if truncated else "MODEL_EMPTY_RESPONSE",
                    "message": (
                        "回答达到长度上限被截断，请把问题拆小一些再试。"
                        if truncated
                        else "模型没有返回可显示的文本。"
                    ),
                    "trace_id": trace_id,
                }
            )
            return

        # 自愈之后仍然不成立的告警，就是这一轮要如实告诉用户的东西
        for item in warnings:
            logger.warning(
                "本轮自检告警 | code=%s 工具=%s 生图任务=%d | %s",
                item.get("code"),
                ",".join(sorted(called_tools)) or "无",
                len(media_task_ids),
                item.get("evidence", ""),
            )

        # 拿不到真实用量（部分本地模型不返回 usage）就退回估算，
        # 但必须把 estimated 标出来，不能让估算值冒充账单
        if turn_usage.calls == 0:
            est_in, est_out = estimate_usage(
                [
                    setup.system_text,
                    *(content for _, content in setup.history),
                    setup.model_text,
                ],
                answer,
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

        # 运营口径：一轮 = 一次用户提问到完整回答。
        # 技能标签：用户命令/选择器 → 具体 key；模型自己 use_skill → auto；
        # 没用技能 → none。三者分开，才能分别算「用户偏好」和「模型命中率」
        skill_label = setup.explicit_skill or ("auto" if setup.skill_box else "none")
        metrics.inc("chat_turns_total", mode=setup.mode, skill=skill_label)
        metrics.observe("chat_turn_duration_seconds", latency_ms / 1000, mode=setup.mode)
        metrics.inc("chat_tokens_total", turn_usage.input_tokens, kind="input")
        metrics.inc("chat_tokens_total", turn_usage.output_tokens, kind="output")
        metrics.inc("chat_model_calls_total", turn_usage.calls)
        if turn_usage.estimated:
            metrics.inc("chat_usage_estimated_total")
        # 价目表里没有这个模型时 cost 是 None（不是 0），不能拿 None 去加
        if turn_usage_dict["cost"] is not None:
            metrics.inc("chat_cost_cny_total", float(turn_usage_dict["cost"]))
        session.messages.append(
            ChatMessage(
                id=message_id,
                role="assistant",
                content=answer,
                created_at=now_iso(),
                media_task_ids=media_task_ids,
                usage=turn_usage_dict,
                latency_ms=latency_ms,
                warnings=warnings,
            )
        )
        self._context.trim(session)
        # 回写本轮实际激活的工具组：discover_tools 可能往里加过东西，
        # 下一轮的 route() 要据此判断「这轮是不是在追问上一个话题」。
        # 用 sorted 而不是原顺序，让会话文件的 diff 稳定
        session.active_groups = sorted(setup.active_box)
        # 技能同理：use_skill 激活的技能下一轮要能绑上它的工具
        session.active_skills = sorted(setup.skill_box)
        await self._sessions.save(session)

        # 回答已落库，后台抽偏好；失败不影响本轮，也不阻塞 SSE
        if self._profile is not None:
            self._spawn(
                self._profile.maybe_suggest(
                    setup.ctx.user_id,
                    setup.model_text,
                    [*setup.history, ("assistant", answer)],
                )
            )

        logger.info(
            "本轮完成 | mode=%s 回答=%d 字 finish=%s 工具调用=%d 生图任务=%d "
            "tokens=%d/%d 成本=%s 重试=%d 估算=%s",
            setup.mode,
            len(answer),
            outcome.finish_reason,
            outcome.tool_calls,
            len(media_task_ids),
            turn_usage.input_tokens,
            turn_usage.output_tokens,
            turn_usage_dict["cost"],
            setup.retry_stats.retries,
            turn_usage.estimated,
        )

        await state.emit(
            {
                "type": "end",
                "message_id": message_id,
                "finish_reason": outcome.finish_reason,
                "trace_id": trace_id,
                "usage": {"turn": turn_usage_dict, "session": dict(session.usage)},
                "latency_ms": latency_ms,
                "tool_retries": setup.retry_stats.to_dict(),
                "tool_budget": setup.tool_budget.to_dict(),
                # 本轮自检告警。前端必须显示出来——用户看到的「已完成」
                # 和实际发生的事不一致时，这是唯一能纠正它的地方
                "warnings": warnings,
                # 本轮是否读到过注入内容、拦了几次危险操作。前端不展示，
                # 但排查「为什么写文件没执行」时这是唯一线索
                "taint": setup.taint.to_dict(),
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

    async def _graph_for(self, tools: list[Any]) -> tuple[Any, str]:
        """取（必要时构建）绑定指定工具集合的图。

        缓存键是工具名集合，所以「同一话题连续追问」只建一次图；
        换话题时多一张，用 ``_MAX_GRAPHS`` 兜住上限——每张图都持有一次
        ``bind_tools`` 的产物，无上限地存下去就是内存泄漏。
        """
        key = frozenset(tool.name for tool in tools)
        async with self._graph_lock:
            cached = self._graphs.get(key)
            if cached is not None:
                return cached
            # 复用已探测到的模式，避免每换一次工具子集就重新探测一遍模型能力
            graph, mode = build_agent_graph(self._settings, mode=self._mode or None, tools=tools)
            self._mode = mode
            if len(self._graphs) >= _MAX_GRAPHS:
                self._graphs.pop(next(iter(self._graphs)))
            self._graphs[key] = (graph, mode)
            return graph, mode
