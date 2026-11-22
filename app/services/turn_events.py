"""一轮对话的事件与文本工具：SSE 帧、工具分类、回答抽取。

这里只放**纯函数**：输入输出都是普通对象，不碰 StreamState、不碰网络。
放在单独模块是为了让 ``tests/test_context.py`` 这类用例不必构造整个 ChatService
就能钉住「空回答怎么判」「工具归哪一类」这些细节。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage

# 工具名 → 契约中的 category
TOOL_CATEGORY = {
    "get_weather": "info",
    "plan_route": "info",
    "search_poi": "info",
    "estimate_budget": "plan",
    "optimize_day_route": "plan",
    "recommend_stay": "plan",
    "recommend_transport": "plan",
    "plan_trip": "plan",
    "export_trip_pdf": "plan",
    "render_trip_map": "plan",
    "list_dir": "system",
    "read_file": "system",
    "open_file": "system",
    "write_file": "system",
    "create_dir": "system",
    "delete_path": "system",
    "set_wallpaper": "system",
    "read_document": "docs",
    "digest_documents": "docs",
    "query_sheet": "docs",
    "aggregate_sheet": "docs",
    "write_sheet": "docs",
    "edit_document": "docs",
    "organize_documents": "docs",
    # 元工具单独一类：它不是业务能力，前端按 category 分组展示时不该混进去
    "discover_tools": "meta",
    "use_skill": "meta",
    "search_knowledge": "knowledge",
    "search_web": "search",
    "fetch_url": "search",
    "parse_shared_link": "plugin",
    "search_social_session": "plugin",
    "generate_scraper": "plugin",
    "generate_image": "media",
    "list_image_templates": "media",
}
SUMMARY_LIMIT = 200
OTHER_CATEGORY = "other"


def tool_category(tool_name: str) -> str:
    """工具名 → 契约里的 category。工具清单与 SSE 事件共用，避免两边不一致。

    技能工具按命名前缀归类：技能是用户可扩展的，不可能逐个登记到表里，
    而前缀由加载器强制加上，比别名可靠。
    """
    if (tool_name or "").startswith("skill__"):
        return "skill"
    return TOOL_CATEGORY.get(tool_name, OTHER_CATEGORY)


def sse(payload: dict[str, Any]) -> str:
    """一条 SSE 帧。前端按空行切帧，所以结尾必须是两个换行。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def as_text(content: Any) -> str:
    """把 LangChain 的 content（str / 内容块列表）拼成纯文本。

    列表里混着 str 与 ``{"type": "text", "text": ...}`` 两种形态，
    只认后者会把纯字符串块静默丢掉。
    """
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


def truncate(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def summarize(output: Any) -> str:
    """工具输出 → 给前端卡片看的一句话摘要。"""
    content = getattr(output, "content", output)
    return truncate(as_text(content) or str(content))


def extract_answer(final_state: dict | None, streamed: str) -> tuple[str, str]:
    """取最终回答。

    structured 模式下流式内容是内部 JSON 决策，不能直接用，
    因此优先从最终状态里取最后一条「不含 tool_calls 的 AI 消息」。
    """
    if final_state:
        # 全程没有可显示文本时也要把 finish_reason 带出去：上层靠它区分
        # 「被长度上限截断」和「上游什么都没给」，两句话对用户的意义完全不同
        empty: tuple[str, str] | None = None
        for message in reversed(final_state.get("messages") or []):
            if not isinstance(message, AIMessage) or getattr(message, "tool_calls", None):
                continue
            text = as_text(message.content)
            meta = getattr(message, "response_metadata", None) or {}
            reason = "length" if meta.get("finish_reason") == "length" else ""
            if text.strip():
                return text, reason or "stop"
            if empty is None:
                empty = (text, reason or "stop")
        if empty is not None:
            return empty
    return streamed, "stop"
