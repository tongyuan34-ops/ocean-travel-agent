"""按 URL 抓正文适配器（SPEC 4.7）。

`selectable_by_llm = False` 是刻意的：它的入参是**一个具体链接**，
不是检索词。若被放进「让模型自选的检索源目录」，模型会拿检索词来调它，
只会得到垃圾结果；它只应在用户给出具体 URL 时被调用。

SSRF 校验、逐跳跳转检查、限长与超时统一由 core.net 负责，本模块只做正文抽取。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import html as lxml_html
from lxml.etree import ParserError

from app.adapters.base import SourceAdapter
from app.core.config import Settings
from app.core.models import Doc, SourceKind, source_kind_value
from app.core.net import fetch_text

# 正文进入上下文前的上限（会话上下文另有截断，这里先做一次）
MAX_CONTENT_CHARS = 8000
_SNIPPET_CHARS = 200

# 需要整块丢弃的标签（导航、脚本、样式对正文毫无价值，还会污染上下文）
_DROP_TAGS = "//script|//style|//noscript|//svg|//iframe|//template|//nav|//footer"
_META_KEYS = {
    "og:title",
    "og:description",
    "og:image",
    "og:site_name",
    "og:type",
    "twitter:title",
    "twitter:description",
    "twitter:image",
    "description",
    "keywords",
}


@dataclass
class FetchedPage:
    """一次网页抓取的结果，正文抓取与链接解析共用。"""

    url: str
    title: str = ""
    text: str = ""
    content_type: str = ""
    status: int = 0
    meta: dict[str, str] = field(default_factory=dict)


async def fetch_page(url: str, settings: Settings) -> FetchedPage:
    """抓取网页并抽取标题、正文与 meta。

    可复用于 fetch_url 与 parse_shared_link，避免两处各写一遍抽取逻辑。
    """
    final_url, raw, ctype, status = await fetch_text(
        url,
        timeout=settings.fetch_timeout_sec,
        max_bytes=settings.max_fetch_bytes,
    )
    meta = extract_meta(raw)
    title, text = extract_readable(raw, ctype)
    if meta.get("og:title") or meta.get("twitter:title"):
        title = meta.get("og:title") or meta.get("twitter:title") or title
    if len(text) > MAX_CONTENT_CHARS:
        text = f"{text[:MAX_CONTENT_CHARS]}\n…（正文过长已截断，原文共 {len(text)} 字）"
    return FetchedPage(
        url=final_url,
        title=title,
        text=text,
        content_type=ctype,
        status=status,
        meta=meta,
    )


class WebFetchAdapter(SourceAdapter):
    name = "web_fetch"
    kind = SourceKind.WEB
    platform = "网页正文"
    selectable_by_llm = False

    def available(self) -> bool:
        # 无外部凭证，恒可用
        return True

    async def _search(self, query: str, limit: int) -> list[Doc]:
        """入参按约定是 http(s) 链接；非链接会由 core.net 拒绝并抛错。"""
        page = await fetch_page(query, self._settings)
        return [
            Doc(
                title=page.title or page.url,
                url=page.url,
                snippet=page.text[:_SNIPPET_CHARS],
                content=page.text,
                source=self.name,
                source_kind=source_kind_value(self.kind),
                platform=self.platform,
                extras={"status": page.status, "content_type": page.content_type},
            )
        ]

    async def fetch(self, url: str) -> FetchedPage:
        """供工具直接调用，返回页面而不是 Doc。"""
        return await fetch_page(url, self._settings)


# ---------- HTML 抽取 ----------


def extract_meta(text: str) -> dict[str, str]:
    """抽取 OpenGraph / Twitter / description 等 meta。

    分享链接（抖音、小红书等）的标题、文案、封面基本都在这几个字段里，
    这是「只处理用户已获得的内容」范围内最稳的抽取方式。
    """
    tree = _parse(text)
    if tree is None:
        return {}
    result: dict[str, str] = {}
    for node in tree.xpath("//meta"):
        key = (node.get("property") or node.get("name") or "").strip().lower()
        if key not in _META_KEYS or key in result:
            continue
        value = _squeeze(node.get("content") or "")
        if value:
            result[key] = value
    return result


def extract_readable(text: str, content_type: str) -> tuple[str, str]:
    """返回 (标题, 正文文本)。非 HTML 内容直接按文本处理。"""
    if not _looks_like_html(text, content_type):
        return "", text.strip()

    tree = _parse(text)
    if tree is None:
        return "", text.strip()

    title = _squeeze(tree.findtext(".//title") or "")
    for node in tree.xpath(_DROP_TAGS):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)

    body = tree.find(".//body")
    raw = (body if body is not None else tree).text_content()
    return title, _squeeze_lines(raw)


def _parse(text: str):
    try:
        return lxml_html.fromstring(text)
    except (ValueError, TypeError, ParserError):
        return None


def _looks_like_html(text: str, content_type: str) -> bool:
    if "html" in (content_type or "").lower() or "xml" in (content_type or "").lower():
        return True
    return "<html" in text[:600].lower() or "<!doctype" in text[:200].lower()


def _squeeze(text: str) -> str:
    return " ".join(text.split())


def _squeeze_lines(text: str) -> str:
    """按行整理正文：去空行、压缩行内空白，保留段落换行。"""
    lines = [_squeeze(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
