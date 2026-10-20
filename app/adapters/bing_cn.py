"""必应中国版适配器（SPEC 4.7）。

两条路线：
1. 配了 BING_SEARCH_API_KEY → 走官方 API（稳定、不会被风控）
2. 没配或调用失败 → 回退抓取 cn.bing.com 的 HTML 结果页

HTML 路线内置 1.2 秒进程内限速——这是刻意加的：高频抓取会立刻触发风控，
一旦识别到验证页就**直接抛错，不做静默重试**，因为静默重试只会加重风控，
还会让用户误以为检索正常、结果可信。
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote_plus

import httpx
from lxml import html as lxml_html
from lxml.etree import ParserError

from app.adapters.base import SourceAdapter
from app.core.models import Doc, SourceKind, source_kind_value
from app.core.net import fetch_text

logger = logging.getLogger(__name__)

_API_ENDPOINT = "https://api.bing.microsoft.com/v7.0/search"
_HTML_ENDPOINT = "https://cn.bing.com/search"
_HTML_MIN_INTERVAL = 1.2
_MAX_LIMIT = 10

# 验证页特征串——命中即说明被风控拦截，结果不可信
_VERIFY_MARKERS = (
    "captcha",
    "verify you are human",
    "验证码",
    "异常流量",
    "unusual traffic",
)

# 进程内限速状态（同一进程内所有 HTML 抓取共用）
_html_lock = asyncio.Lock()
_last_html_call = 0.0


class BingCnAdapter(SourceAdapter):
    name = "bing_cn"
    kind = SourceKind.GENERAL_CN
    platform = "必应（中国）"

    def available(self) -> bool:
        # 无 Key 也能回退 HTML 抓取，因此始终可用
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _search(self, query: str, limit: int) -> list[Doc]:
        limit = max(1, min(limit, _MAX_LIMIT))

        if self._settings.bing_search_api_key:
            try:
                docs = await self._search_api(query, limit)
                if docs:
                    return docs
                logger.warning("必应 API 未返回结果，回退 HTML 抓取 | q=%s", query)
            except Exception as exc:  # noqa: BLE001 - API 失败要回退，不直接降级
                logger.warning("必应 API 调用失败，回退 HTML 抓取：%s", exc)

        return await self._search_html(query, limit)

    # ---------- API 路线 ----------

    async def _search_api(self, query: str, limit: int) -> list[Doc]:
        params = {
            "q": query,
            "count": limit,
            "mkt": "zh-CN",
            "setLang": "zh-Hans",
            "responseFilter": "Webpages",
        }
        headers = {"Ocp-Apim-Subscription-Key": self._settings.bing_search_api_key}
        async with httpx.AsyncClient(timeout=self.timeout()) as client:
            resp = await client.get(_API_ENDPOINT, params=params, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"必应 API 返回 HTTP {resp.status_code}")

        pages = ((resp.json().get("webPages") or {}).get("value")) or []
        docs: list[Doc] = []
        for item in pages[:limit]:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            snippet = _squeeze(item.get("snippet") or "")
            docs.append(
                Doc(
                    title=_squeeze(item.get("name") or "") or url,
                    url=url,
                    snippet=snippet,
                    content=snippet,
                    source=self.name,
                    source_kind=source_kind_value(self.kind),
                    platform=self.platform,
                    published_at=item.get("dateLastCrawled") or "",
                    extras={"route": "api"},
                )
            )
        return docs

    # ---------- HTML 回退路线 ----------

    async def _search_html(self, query: str, limit: int) -> list[Doc]:
        await _throttle()
        url = f"{_HTML_ENDPOINT}?q={quote_plus(query)}&count={_MAX_LIMIT}&mkt=zh-CN"
        final_url, text, _ctype, _status = await fetch_text(
            url, timeout=self.timeout()
        )

        if "captcha" in final_url.lower() or _is_verify_page(text):
            raise RuntimeError(
                "触发必应验证页，HTML 抓取已被拦截；建议配置 BING_SEARCH_API_KEY 走官方 API"
            )

        return _parse_results(text, limit, self.name, self.kind, self.platform)


async def _throttle() -> None:
    """HTML 路线的进程内限速：距上次调用不足 1.2 秒就等一等。"""
    global _last_html_call
    async with _html_lock:
        elapsed = time.monotonic() - _last_html_call
        if elapsed < _HTML_MIN_INTERVAL:
            await asyncio.sleep(_HTML_MIN_INTERVAL - elapsed)
        _last_html_call = time.monotonic()


def _is_verify_page(text: str) -> bool:
    head = text[:4000].lower()
    return any(marker in head for marker in _VERIFY_MARKERS)


def _parse_results(
    text: str, limit: int, source: str, kind: SourceKind, platform: str
) -> list[Doc]:
    """解析 cn.bing.com 结果页的 li.b_algo 区块。"""
    try:
        tree = lxml_html.fromstring(text)
    except (ValueError, TypeError, ParserError) as exc:
        raise RuntimeError(f"必应结果页解析失败：{exc}") from exc

    docs: list[Doc] = []
    for block in tree.xpath("//li[contains(@class, 'b_algo')]"):
        links = block.xpath(".//h2/a")
        if not links:
            continue
        href = (links[0].get("href") or "").strip()
        if not href.startswith("http"):
            continue

        snippet = ""
        for paragraph in block.xpath(".//p"):
            snippet = _squeeze(paragraph.text_content())
            if snippet:
                break
        if not snippet:
            snippet = _squeeze(block.text_content())[:200]

        docs.append(
            Doc(
                title=_squeeze(links[0].text_content()) or href,
                url=href,
                snippet=snippet,
                content=snippet,
                source=source,
                source_kind=source_kind_value(kind),
                platform=platform,
                extras={"route": "html"},
            )
        )
        if len(docs) >= limit:
            break
    return docs


def _squeeze(text: str) -> str:
    return " ".join(text.split())
