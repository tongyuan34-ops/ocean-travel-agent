"""LangGraph agent 循环。

两种模式（SPEC 4.4）：
- native：模型支持原生 tool calling → agent 节点 + 工具节点循环
- structured：不支持（如本地小模型）→ 让模型输出 JSON 决策，由代码执行工具

两种模式对外行为一致，上层不需要区分。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError

from app.agent.model import build_chat_model, detect_tool_calling
from app.agent.permissions import PermissionDenied
from app.agent.prompts import build_system_prompt
from app.agent.resilience import RetryPolicy, make_all_resilient
from app.agent.state import AgentState
from app.core.config import Settings
from app.mcp.registry import all_tools

logger = logging.getLogger(__name__)

# 单轮对话内最多执行多少步，防止工具循环失控
MAX_STEPS = 12


# 追踪回调**不能**用 ``with_config`` 绑在模型上。
#
# 绑上去之后，模型会带着自己的 ``config["callbacks"]`` 进入调用，子级配置会
# 顶掉 ``astream_events`` 注入的 streamer——``on_chat_model_start/stream/end``
# 整组消失（连一个普通 Spy 回调也一样，与 Langfuse 无关）。症状极具迷惑性：
# 回答正常生成并落库，但前端一个 token 都收不到，用量也只剩估算值。
# 桌面版默认开启 Langfuse，所以稳定复现；源码环境默认不开，看起来像「打包问题」。
#
# 正确做法：回调统一放进 ``trace_config`` 的 config 里（见 app/core/tracing.py），
# 它会随运行树传给每一个子 run，chain / tool / model 三层都能覆盖。


# 系统提示词已按「对话风格」拆到 app/agent/prompts.py：
# 核心规则（可信与安全）+ 领域规则（只有旅行类人格才注入）+ 说话方式。
# 这里保留一个默认风格的别名，方便「只想看看提示词长什么样」的场景。
SYSTEM_PROMPT = build_system_prompt()


def _handle_tool_error(exc: Exception) -> str:
    """工具抛异常时的兜底：转成错误文本回给模型，而不是让整轮对话失败。

    LangGraph 的默认处理器只接住 ``ToolInvocationError``（参数不合法），
    其余异常一律重新抛出。从库的角度这很合理——别替使用者吞掉未知错误；
    但对一个聊天应用，某个工具的一行代码缺陷不该让用户的整轮对话 500，
    前端连一句「哪里出了问题」都拿不到。

    两条边界必须守住：

    - ``logger.exception`` 不能省。转成文本等于把异常降级成返回值，
      不打日志就再也没有人会发现这个 bug；
    - ``asyncio.CancelledError`` 是 ``BaseException``，本来就不会进这里，
      「用户点停止」仍然立即生效，不会被兜底逻辑拦住。

    返回的 ``TOOL_CRASHED`` 前缀和 ``TOOL_FAILED`` 一样被「工具返回错误码」那条规则覆盖：
    模型看到它就必须承认「这次没拿到数据」，不许编造。

    唯一例外是 ``PermissionDenied``：那是**用户做的选择**，不是缺陷，
    所以既不该打 traceback，也不该说成「崩溃」（见下方分支）。
    """
    if isinstance(exc, ToolInvocationError):
        # 参数校验失败是模型的问题，原文回给它最有信息量
        return exc.message
    if isinstance(exc, PermissionDenied):
        # 拒绝是**预期结果**，不是崩溃。早先它落进下面的 TOOL_CRASHED 分支，
        # 后果有两个：每次用户点「拒绝」都留一条带 traceback 的 ERROR 日志
        # （真正的崩溃会被淹掉）；评测也把它算成「工具真的执行了」，
        # 于是「禁用工具真的执行」这个指标失真。
        # 单独给一个 TOOL_DENIED 前缀，日志降为 info，评测据此归到「被拦下」。
        logger.info("工具调用被拒绝 | %s", exc.message)
        return f"TOOL_DENIED：{exc.message}"
    logger.exception("工具执行抛出未预期异常 | 类型=%s", type(exc).__name__)
    return f"TOOL_CRASHED：{type(exc).__name__}: {exc}"


def build_agent_graph(
    settings: Settings,
    mode: str | None = None,
    tools: list[BaseTool] | None = None,
    model: BaseChatModel | None = None,
) -> tuple[Any, str]:
    """构造 agent 图。

    返回 (编译后的图, 实际使用的模式)，模式为 'native' 或 'structured'。

    ``model`` 只给评测用：自测时注入脚本化模型，避免依赖真实 API。
    """
    chat_model = model if model is not None else build_chat_model(settings)
    # 内置工具 + 运行时接入的 MCP 外部工具。没有配 MCP_SERVERS 时后者为空，
    # 所以这个调用等价于原来的 ALL_TOOLS，但给「装一个服务端就多一批工具」留了口子
    tool_list = tools if tools is not None else all_tools()
    # 统一套一层重试：工具抖动是可恢复的，不该直接甩给模型去猜
    resilient = make_all_resilient(
        tool_list,
        RetryPolicy(
            attempts=settings.tool_retry_attempts,
            base_delay=settings.tool_retry_base_delay,
        ),
    )
    resolved = mode or detect_tool_calling(chat_model, settings)

    if resolved == "native":
        graph = _build_native(chat_model, resilient)
    else:
        graph = _build_structured(chat_model, resilient)

    return graph.compile(), resolved


# ---------- native 模式 ----------


def _build_native(model: BaseChatModel, tools: list[BaseTool]) -> StateGraph:
    # 只绑工具，不绑回调：回调一旦绑在可运行对象上，模型级事件会被顶掉，
    # 逐字 token 与真实用量一起消失（原因见 MAX_STEPS 下面的注释）
    bound = model.bind_tools(tools)

    async def agent_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        # 必须是异步 + 显式 astream，不能用同步 invoke：
        #
        # 1. 同步节点会被 LangGraph 放进线程池执行，回调上下文（ContextVar）
        #    很多环境下传不进那个线程，``astream_events`` 就收不到
        #    ``on_chat_model_stream``——回答明明生成完了、也落库了，前端却
        #    一个字都收不到，最后只能报 MODEL_EMPTY_RESPONSE。实测打包版稳定
        #    复现，源码环境偶发，正是这种「靠父级回调顺带打开流式」的脆弱点。
        # 2. ``invoke`` 能否吐 token 取决于 ``_should_stream`` 去猜父级回调，
        #    显式 ``astream`` 不依赖这个猜测，任何环境下都能逐块产出。
        #
        # 顺带修好用量：回调不再丢失，``on_chat_model_end`` 里的真实 usage
        # 才能记上，而不是退回「按字数估算」。
        chunks: list[Any] = []
        async for chunk in bound.astream(state["messages"]):
            chunks.append(chunk)
        if not chunks:
            # 上游没给任何块时退回一次性调用，避免把空消息写进状态
            return {"messages": [await bound.ainvoke(state["messages"])]}
        merged: Any = chunks[0]
        for chunk in chunks[1:]:
            merged = merged + chunk
        return {"messages": [merged]}

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=_handle_tool_error))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder


# ---------- structured 模式 ----------


def _build_structured(model: BaseChatModel, tools: list[BaseTool]) -> StateGraph:
    catalog = _tool_catalog(tools)
    # 回调同样不绑在模型上：它由调用方通过 config 传进来（trace_config），
    # 这里每一次 ``ainvoke`` 都会从上下文继承，循环里的每一趟都在追踪范围内
    traced = model

    async def agent_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        messages = list(state["messages"])
        for step in range(MAX_STEPS):
            prompt = _instruction(catalog, first=(step == 0))
            reply = await traced.ainvoke([*messages, HumanMessage(content=prompt)])
            decision = _parse_decision(reply.content)

            if decision is None or "final" in decision:
                final = ""
                if decision and decision.get("final"):
                    final = str(decision["final"])
                return {"messages": [AIMessage(content=final or _as_text(reply.content))]}

            tool = _find_tool(tools, str(decision.get("tool") or ""))
            if tool is None:
                return {
                    "messages": [
                        AIMessage(content=f"我尝试调用了一个不存在的工具：{decision.get('tool')}")
                    ]
                }

            args = decision.get("args") or {}
            try:
                result = await tool.ainvoke(args)
            except Exception as exc:  # noqa: BLE001 - 工具失败要如实告知
                result = f"工具执行失败：{exc}"

            messages.append(reply)
            messages.append(HumanMessage(content=f"工具「{tool.name}」返回：\n{result}"))

        return {"messages": [AIMessage(content="已达到最大步数，未能给出最终答案。")]}

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder


# ---------- 辅助函数 ----------


def _tool_catalog(tools: list[BaseTool]) -> str:
    lines = []
    for tool in tools:
        lines.append(f"- {tool.name}：{tool.description}")
    return "\n".join(lines)


def _instruction(catalog: str, *, first: bool) -> str:
    head = "可用工具：" if first else "如需继续调用工具，可用工具如下："
    return (
        f"{head}\n{catalog}\n\n"
        "请输出一个 JSON，二选一：\n"
        '需要调用工具：{"tool": "工具名", "args": {"参数名": "值"}}\n'
        '可以直接回答：{"final": "给用户的完整回答"}\n'
        "只输出 JSON，不要输出其他内容。"
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_decision(content: Any) -> dict | None:
    """从模型输出里抽出 JSON 决策。"""
    text = _as_text(content)
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    return str(content or "")


def _find_tool(tools: list[BaseTool], name: str) -> BaseTool | None:
    for tool in tools:
        if tool.name == name:
            return tool
    return None
