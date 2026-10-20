"""Agent 状态定义。"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """对话状态。

    messages 用 add_messages 归约：节点只需返回新增的消息，
    LangGraph 会自动追加并按 id 合并。
    """

    messages: Annotated[list[BaseMessage], add_messages]
