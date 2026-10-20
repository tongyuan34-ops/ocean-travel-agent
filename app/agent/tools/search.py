"""search 组工具：通用网页检索与按 URL 抓正文（SPEC 4.6）。

两者都走适配器层（app/adapters），工具自身不发请求——
统一复用适配器的「可用性判断 + 超时 + 单源失败降级」逻辑，
避免每个工具各写一套重试与超时。

输出首行固定是检索统计（命中源数 / 各源命中数 / 耗时 / 是否降级或 mock），
因为契约要求 search 组工具的 tool_result summary 必须含这几项。
"""

from __future__ import annotations

import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.adapters import find
from app.adapters.web_fetch import WebFetchAdapter
from app.core.context import truncate_tool_output
from app.core.net import FetchError, UrlNotAllowed

_MAX_LIMIT = 10
_LIST_LIMIT = 2200


class SearchWebInput(BaseModel):
    query: str = Field(description="检索关键词，如「三亚 5 天自由行 攻略」")
    limit: int = Field(default=5, description="返回条数上限，1..10")


@tool(args_schema=SearchWebInput)
async def search_web(query: str, limit: int = 5) -> str:
    """检索公开网页信息（必应中国版），用于获取攻略、开放时间、交通等公开资料。

    返回标题、链接与摘要，请在回答中标注来源链接，不得把检索内容当成自身知识。
    """
    query = (query or "").strip()
    if not query:
        return "INVALID_REQUEST：检索关键词不能为空"

    adapter = find("bing_cn")
    if adapter is None or not adapter.available():
        return "TOOL_NOT_ENABLED：通用网页检索未启用（请在 .env 配置 BING_SEARCH_API_KEY）"

    limit = max(1, min(limit, _MAX_LIMIT))
    started = time.perf_counter()
    docs = await adapter.search(query, limit)
    elapsed = time.perf_counter() - started

    platform = adapter.platform
    real = [doc for doc in docs if not doc.mock]
    degraded = [doc for doc in docs if doc.mock]

    if not docs:
        return (
            f"SEARCH_FAILED：未能检索到「{query}」的结果（{platform} 未返回内容）。"
            "请如实告知用户查不到，不要凭记忆编造。"
        )

    lines = [f"检索「{query}」（数据来源：{platform}）"]
    lines.append(
        f"统计：命中源数 {1 if real else 0}｜各源命中数 {adapter.name}={len(real)}｜"
        f"耗时 {elapsed:.2f}s｜降级 {'是' if degraded else '否'}"
    )

    if degraded and not real:
        reason = degraded[0].extras.get("reason") or "未知原因"
        lines.append("")
        lines.append(f"⚠️ 本次检索未获得真实结果（原因：{reason}）。")
        lines.append(
            "请如实告知用户本次检索失败，不要编造任何检索内容作为事实。"
        )
        return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)

    lines.append("")
    for index, doc in enumerate(real, start=1):
        lines.append(f"{index}. {doc.title}")
        lines.append(f"   链接：{doc.url}")
        if doc.snippet:
            lines.append(f"   摘要：{doc.snippet}")
    if degraded:
        lines.append("")
        lines.append("（部分源已降级，以上结果未包含降级源的内容）")
    lines.append("")
    lines.append("引用要求：以上链接来自真实检索结果，回答中引用时请附上对应链接。")
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)


class FetchUrlInput(BaseModel):
    url: str = Field(description="要抓取正文的完整 http(s) 链接，不是检索词")


@tool(args_schema=FetchUrlInput)
async def fetch_url(url: str) -> str:
    """按用户给出的完整链接抓取网页正文，用于阅读具体页面内容。

    只接受 http(s) 链接；内网、本机地址与 file:// 等协议会被拒绝。
    """
    adapter = find("web_fetch")
    if not isinstance(adapter, WebFetchAdapter):
        return "TOOL_NOT_ENABLED：网页正文抓取未启用"

    try:
        page = await adapter.fetch(url)
    except UrlNotAllowed as exc:
        return f"{exc.code}：{exc.message}"
    except FetchError as exc:
        return f"{exc.code}：{exc.message}"

    if not page.text.strip():
        return f"FETCH_FAILED：{page.url} 未取到可读正文（可能是图片页或空页面）"

    lines = [
        f"网页正文：{page.title or page.url}",
        f"链接：{page.url}",
        f"（数据来源：抓取自该链接原文，HTTP {page.status}）",
        "",
        page.text,
    ]
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)
