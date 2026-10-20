"""数据源适配器层（SPEC 4.7）。

对外只暴露 registry 的查询函数；适配器实例由 registry 统一构造与持有。
"""

from __future__ import annotations

from app.adapters.base import SourceAdapter
from app.adapters.bing_cn import BingCnAdapter
from app.adapters.mediacrawler import (
    PLATFORM_LABELS,
    SOCIAL_PLATFORM_KEYS,
    MediaCrawlerAdapter,
    collect_docs,
    run_crawler_subprocess,
)
from app.adapters.registry import (
    available_platforms,
    build_adapters,
    find,
    get_adapters,
    llm_selectable,
    reset,
    social_adapters,
)
from app.adapters.web_fetch import FetchedPage, WebFetchAdapter, fetch_page

__all__ = [
    "BingCnAdapter",
    "FetchedPage",
    "MediaCrawlerAdapter",
    "PLATFORM_LABELS",
    "SOCIAL_PLATFORM_KEYS",
    "SourceAdapter",
    "WebFetchAdapter",
    "available_platforms",
    "build_adapters",
    "collect_docs",
    "fetch_page",
    "find",
    "get_adapters",
    "llm_selectable",
    "reset",
    "run_crawler_subprocess",
    "social_adapters",
]
