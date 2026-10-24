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

from app.agent.model import build_chat_model, detect_tool_calling
from app.agent.state import AgentState
from app.agent.tools import ALL_TOOLS
from app.core.config import Settings

logger = logging.getLogger(__name__)

# 单轮对话内最多执行多少步，防止工具循环失控
MAX_STEPS = 12

SYSTEM_PROMPT = (
    "你是旅行规划助手。你可以调用工具查询天气、检索地点、规划路线。\n"
    "规则：\n"
    "1. 涉及天气、地点、路线、距离等事实信息时，必须先调用工具获取，"
    "严禁凭记忆编造；工具返回「TOOL_NOT_ENABLED」或失败时，如实告知用户，不要猜测。\n"
    "2. 涉及价格、班次、余票等实时信息时，说明需以官方渠道为准，不要给出具体数字。\n"
    "3. 回复中不要标注「数据来源：高德开放平台」之类的来源标签；"
    "只有 search_web 的检索结果需要附链接（见第 5 条）。\n"
    "4. 调用 plan_route 时必须传入 city 参数（出发地所在城市），"
    "否则「三亚市区」这类模糊地名可能被解析到错误位置；"
    "若工具返回「数据异常」或「无法解析」，改用更具体的地名重试，"
    "重试后仍失败就如实告知用户查不到，不要猜。\n"
    "5. 需要外部公开资料（攻略、开放时间、交通政策等）时用 search_web 检索，"
    "回答中必须附上检索结果里的链接作为来源，不得把检索摘要改写成自身知识。\n"
    "6. 用户粘贴了具体链接（如抖音/小红书分享链接）时用 parse_shared_link 解析；"
    "需要阅读用户给出的某个具体网页正文时用 fetch_url。"
    "这两个工具只处理用户给出的链接，不得拿它们去当关键词搜索引擎用。\n"
    "7. 检索结果标注「降级」「SEARCH_FAILED」或 parse_shared_link 失败时，"
    "必须如实告知用户本次没拿到真实资料，不要用常识补足。\n"
    "8. 用户明确要求抖音、B站等平台的站内内容时，直接调用 search_social_session 采集；"
    "部分平台未配置 Cookie 时，工具会自动跳过并继续采集其余平台，"
    "不得因此整体拒绝，也不得在调用前断言插件关闭或用 search_web 冒充平台内容；"
    "只有工具实际返回 TOOL_NOT_ENABLED 时，才说明需要在 .env 开启开关并配置 Cookie。"
    "generate_scraper 只生成脚本草案，绝不要说成「已经帮你采集/执行了」。\n"
    "9. generate_image 的图片由前端自动轮询并在对话中直接展示，用户可在页面内放大与保存；"
    "不要声称无法保存图片，也不要让用户去打开本地文件。\n"
    "10. 用户要求查看或操作文件时，先用 list_dir 列出目录、再用 read_file 读取；"
    "需要用户在外部程序里编辑时用 open_file 打开，用户保存后重新 read_file 取最新内容；"
    "write_file/create_dir/delete_path/open_file 执行时会弹权限确认。"
    "不得预先声称无法打开或操作文件；路径必须在 ALLOWED_ROOTS 白名单内，"
    "越界时如实说明白名单范围，不要编造文件内容。\n"
    "11. 用中文回答，条理清晰。"
)


def build_agent_graph(
    settings: Settings, mode: str | None = None, tools: list[BaseTool] | None = None
) -> tuple[Any, str]:
    """构造 agent 图。

    返回 (编译后的图, 实际使用的模式)，模式为 'native' 或 'structured'。
    """
    model = build_chat_model(settings)
    tool_list = tools if tools is not None else ALL_TOOLS
    resolved = mode or detect_tool_calling(model, settings)

    if resolved == "native":
        graph = _build_native(model, tool_list)
    else:
        graph = _build_structured(model, tool_list)

    return graph.compile(), resolved


# ---------- native 模式 ----------


def _build_native(model: BaseChatModel, tools: list[BaseTool]) -> StateGraph:
    bound = model.bind_tools(tools)

    def agent_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        return {"messages": [bound.invoke(state["messages"])]}

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else END

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")
    return builder


# ---------- structured 模式 ----------


def _build_structured(model: BaseChatModel, tools: list[BaseTool]) -> StateGraph:
    catalog = _tool_catalog(tools)

    async def agent_node(state: AgentState) -> dict[str, list[BaseMessage]]:
        messages = list(state["messages"])
        for step in range(MAX_STEPS):
            prompt = _instruction(catalog, first=(step == 0))
            reply = await model.ainvoke([*messages, HumanMessage(content=prompt)])
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
