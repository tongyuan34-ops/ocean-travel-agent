"""plugin 组 A：用户主动粘贴的分享链接解析（SPEC 4.6 / 2.5 第三类）。

**边界**：只解析用户在会话中显式粘贴的那一条链接。
不主动遍历、不批量抓取、不绕过登录、不伪造签名、不高频请求——
这属于「处理用户已经获得的内容」，因此允许默认启用、可随开源版本发布。

实现方式是最通用也最稳的一种：抓取该链接页面，读它的 OpenGraph/Twitter meta
（标题、文案、封面）。抖音、小红书这类分享页都会带这些字段。
页面需要登录态或纯前端渲染拿不到 meta 时，**如实报错，不猜内容**。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.adapters import find
from app.adapters.web_fetch import WebFetchAdapter
from app.core.context import truncate_tool_output
from app.core.net import FetchError, UrlNotAllowed

_LIST_LIMIT = 1600
_MAX_HINTS = 5
_MAX_TAGS = 8

# 域名 → 平台展示名
_PLATFORMS = {
    "douyin.com": "抖音",
    "iesdouyin.com": "抖音",
    "v.douyin": "抖音",
    "xiaohongshu.com": "小红书",
    "xhslink.com": "小红书",
    "bilibili.com": "B站",
    "b23.tv": "B站",
    "weibo.com": "微博",
    "weibo.cn": "微博",
    "zhihu.com": "知乎",
    "tieba.baidu.com": "贴吧",
    "xiaoheihe.cn": "小黑盒",
    "heybox.com": "小黑盒",
    "dianping.com": "大众点评",
    "meituan.com": "美团",
    "ctrip.com": "携程",
}

# 「地点/地址/位置」类显式标注
_PLACE_LABEL = re.compile(
    r"(?:地点|地址|位置|定位)[:：]?\s*([^\s#，。；;、|｜]{2,30})"
)
# 以行政/景点后缀结尾的疑似地名
_PLACE_SUFFIX = re.compile(
    r"([^\s#，。；;、|｜]{2,12}(?:市|区|县|镇|景区|公园|酒店|民宿|机场|车站|海滩|湾|岛|山|湖))"
)
# 话题标签
_HASHTAG = re.compile(r"#([^#\s，。；;]{1,20})#")


class ParseLinkInput(BaseModel):
    url: str = Field(description="用户粘贴的分享链接，需为完整 http(s) 链接")


@tool(args_schema=ParseLinkInput)
async def parse_shared_link(url: str) -> str:
    """解析用户粘贴的分享链接，提取标题、文案、封面与可能的地点线索。

    只处理用户显式给出的这一条链接，不主动检索平台内容。
    页面需要登录态时如实报错，不编造页面内容。
    """
    url = (url or "").strip()
    if not url:
        return "INVALID_REQUEST：请提供要解析的分享链接"

    adapter = find("web_fetch")
    if not isinstance(adapter, WebFetchAdapter):
        return "TOOL_NOT_ENABLED：链接解析未启用"

    try:
        page = await adapter.fetch(url)
    except UrlNotAllowed as exc:
        return f"{exc.code}：{exc.message}"
    except FetchError as exc:
        return f"{exc.code}：{exc.message}"

    meta = page.meta
    title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or page.title
        or ""
    ).strip()
    description = (
        meta.get("og:description") or meta.get("twitter:description") or meta.get("description") or ""
    ).strip()
    cover = (meta.get("og:image") or meta.get("twitter:image") or "").strip()
    platform = _detect_platform(page.url)

    # 页面没有标题也没有文案 → 基本可以判定内容拿不到（登录墙/纯前端渲染）
    if not title and not description:
        return (
            f"LINK_PARSE_FAILED：未能从 {page.url} 解析到标题与文案。"
            "该分享页可能需要登录态或由前端渲染，本工具不绕过登录，"
            "请用户直接粘贴图文内容，或稍后重试。"
        )

    blob = "\n".join([title, description])
    hints = _place_hints(blob)
    tags = _tags(blob)

    lines = [f"分享链接解析（平台：{platform}）"]
    lines.append(f"链接：{page.url}")
    lines.append("")
    lines.append(f"标题：{title or '（未提供）'}")
    lines.append(f"文案：{description or '（未提供）'}")
    if cover:
        lines.append(f"封面：{cover}")
    if tags:
        lines.append(f"话题标签：{'、'.join(tags)}")
    lines.append("")
    if hints:
        lines.append("可能的地点线索（由粘贴内容推断，需用户确认，不得当既定事实）：")
        for hint in hints:
            lines.append(f"- {hint}")
    else:
        lines.append("未从该链接中识别到明确的地点线索，如需具体位置请让用户补充。")
    lines.append("")
    lines.append("说明：以上内容抓取自用户提供的分享页原文，非平台检索结果。")

    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)


# ---------- 辅助 ----------


def _detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for domain, name in _PLATFORMS.items():
        if host == domain or host.endswith(domain) or domain in host:
            return name
    return "通用网页"


def _place_hints(text: str) -> list[str]:
    """从标题与文案里抽疑似地名。

    全部标注为「线索」而非结论——分享文案里的地名常是泛指或玩梗，
    直接当成用户要去的目的地会出大错。
    """
    found: list[str] = []
    for pattern in (_PLACE_LABEL, _PLACE_SUFFIX):
        for match in pattern.finditer(text):
            value = match.group(1).strip()
            if value and value not in found:
                found.append(value)
    return found[:_MAX_HINTS]


def _tags(text: str) -> list[str]:
    seen: list[str] = []
    for match in _HASHTAG.finditer(text):
        tag = match.group(1).strip()
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:_MAX_TAGS]
