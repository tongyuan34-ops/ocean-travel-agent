"""Agent 循环冒烟测试：观察一次对话里模型如何调用工具。

用法（在项目根目录执行）：

    python scripts/test_agent.py "三亚三天怎么玩？"
    python scripts/test_agent.py "三亚今天天气如何" --mode native
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agent.graph import MAX_STEPS, SYSTEM_PROMPT, build_agent_graph
from app.core.config import load_settings


def _describe(message) -> str:
    if isinstance(message, ToolMessage):
        return f"[工具结果] {message.content}"
    if isinstance(message, AIMessage):
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            detail = "，".join(f"{call.get('name')}({call.get('args')})" for call in calls)
            return f"[模型决定调用工具] {detail}"
        return f"[模型回答] {message.content}"
    if isinstance(message, HumanMessage):
        return f"[用户/工具指令] {message.content}"
    return f"[{type(message).__name__}] {message.content}"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 循环冒烟测试")
    parser.add_argument("question", help="要问的问题")
    parser.add_argument(
        "--mode",
        choices=["auto", "native", "structured"],
        default="auto",
        help="tool calling 模式；auto 会先做一次真实探测",
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.model_api_key and "localhost" not in settings.model_base_url:
        print("请先在 .env 里填写 MODEL_API_KEY（DeepSeek）")
        return 1

    mode = None if args.mode == "auto" else args.mode
    graph, resolved = build_agent_graph(settings, mode=mode)
    print(f"模型：{settings.model_name}｜tool calling 模式：{resolved}")
    print(f"问题：{args.question}\n")

    result = await graph.ainvoke(
        {"messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=args.question)]},
        config={"recursion_limit": MAX_STEPS * 2 + 5},
    )

    print("执行轨迹：")
    for message in result["messages"]:
        print("  " + _describe(message))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
