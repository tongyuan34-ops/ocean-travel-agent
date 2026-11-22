"""一趟模型循环：把 agent 图的事件翻译成 SSE 事件，并收齐这一趟的结果。

为什么单独成模块：``run_attempt`` 里每个分支都关系到「用户能不能看见正在发生什么」
（token、工具卡片、权限申请、心跳、取消）。它只依赖图、事件日志和队列，
不需要会话存储、提示词、摘要——把这层依赖剥出来后，SSE 协议的用例可以直接
驱动它，而自愈重跑也只是「再调用一次」。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent.graph import MAX_STEPS
from app.core.errors import TOOL_ERROR_PREFIXES
from app.core.tracing import trace_config
from app.core.usage import TokenUsage, usage_from_output
from app.services.streams import EventLog, StreamState
from app.services.turn_errors import map_failure
from app.services.turn_events import as_text, summarize, tool_category, truncate

logger = logging.getLogger(__name__)

# 跨进程取消只能轮询共享标记。限流到每秒一次：每条 token 都查一次 Redis，
# 延迟和成本都不划算
CANCEL_POLL_SEC = 1.0

_TASK_ID_RE = re.compile(
    r"task_id[：:\s]*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


@dataclass
class Attempt:
    """一趟模型循环的结果。默认值让「什么都没发生」也是合法状态。"""

    streamed: str = ""
    final_state: dict[str, Any] | None = None
    failed: dict[str, Any] | None = None
    media_task_ids: list[str] = field(default_factory=list)
    tool_calls: int = 0
    called_tools: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class TurnRun:
    """多趟尝试之间不变的东西。

    打包传参而不是排七个位置参数：自愈重跑时调用处只写
    ``run_attempt(ctx, messages)``，不会因为参数顺序记错而把
    trace_id 传成 session_id。
    """

    state: StreamState
    graph: Any
    mode: str
    queue: asyncio.Queue
    trace_id: str
    session_id: str
    user_id: str
    turn_usage: TokenUsage
    log: EventLog


async def run_attempt(ctx: TurnRun, messages: list[Any]) -> Attempt:
    """跑一趟模型循环：把图事件转成 SSE 推出去，返回这一趟的结果。

    单独成函数而不是内联，是因为自愈要重跑同一段流程。内联的话第二趟
    只能复制一遍，两处迟早走样——而这段代码里每个分支都关系到
    「用户能不能看见正在发生什么」，复制走样的代价是静默丢事件。
    """
    state = ctx.state
    queue = ctx.queue
    result = Attempt()
    last_cancel_check = 0.0

    # 上一趟若被打断，队列里可能还留着没消费完的事件。
    # 不清干净的话，第二趟会先把上一趟的 token 吐出来
    while not queue.empty():
        queue.get_nowait()

    # 上次检查「别的进程有没有取消我」的时刻。跨进程取消只能轮询共享标记，
    # 限流到每秒一次：每条 token 都查一次 Redis，延迟和成本都不划算
    async def _cancelled_elsewhere() -> bool:
        nonlocal last_cancel_check
        now = time.monotonic()
        if now - last_cancel_check < CANCEL_POLL_SEC:
            return False
        last_cancel_check = now
        if not await ctx.log.is_cancelled(state.stream_id):
            return False
        logger.info("检测到跨进程取消 | stream=%s", state.stream_id)
        state.cancelled = True
        return True

    # 图放在后台任务里跑，图事件与权限请求都送进同一个队列，消费端只从队列取。
    # 否则工具在等用户确认期间会阻塞 astream_events，整条流被卡死
    async def _run_graph() -> None:
        try:
            async for graph_event in ctx.graph.astream_events(
                {"messages": messages},
                version="v2",
                config={
                    "recursion_limit": MAX_STEPS * 2 + 5,
                    # run_name / metadata 会出现在 LangSmith 面板上，
                    # 便于按 session_id 或 trace_id 直接筛出这一轮
                    **trace_config(ctx.trace_id, session_id=ctx.session_id, user_id=ctx.user_id),
                },
            ):
                await queue.put(("graph", graph_event))
        except Exception as exc:  # noqa: BLE001 - 交给消费端统一映射错误码
            await queue.put(("failed", exc))
            return
        await queue.put(("finished", None))

    runner = asyncio.create_task(_run_graph())
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
                result.failed = map_failure(payload)
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
                    piece = as_text(getattr(chunk, "content", chunk))
                    if piece:
                        result.streamed += piece
                        # structured 模式内部会输出 JSON 决策，不能直接透给用户
                        if ctx.mode == "native":
                            await state.emit({"type": "token", "content": piece})

                elif name == "on_tool_start":
                    result.tool_calls += 1
                    result.called_tools.add(event.get("name") or "")
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
                    summary = summarize(output)
                    status = "error" if summary.startswith(TOOL_ERROR_PREFIXES) else "ok"
                    if event.get("name") == "generate_image" and status == "ok":
                        raw = as_text(getattr(output, "content", output)) or str(output)
                        match = _TASK_ID_RE.search(raw)
                        if match and match.group(1) not in result.media_task_ids:
                            result.media_task_ids.append(match.group(1))
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
                            "summary": truncate(str(event["data"].get("error") or "工具执行失败")),
                        }
                    )

                elif name == "on_chat_model_end":
                    # native 模式下每轮工具调用都会多一次模型调用，
                    # 所以要在这里逐次累加，而不是只看最后一条消息
                    found = usage_from_output(event["data"].get("output"))
                    if found is not None:
                        ctx.turn_usage.add(*found)

                elif name == "on_chain_end":
                    output = event["data"].get("output")
                    if isinstance(output, dict) and "messages" in output:
                        result.final_state = output
    finally:
        if not runner.done():
            runner.cancel()

    return result
