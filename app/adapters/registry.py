"""适配器注册表（SPEC 4.7）。

capabilities 里的 `platforms` 直接来自这里 available()=True 的适配器，
字段值与适配器 name 一致。**前端不得硬编码平台清单。**
"""

from __future__ import annotations

from app.adapters.base import SourceAdapter
from app.adapters.bing_cn import BingCnAdapter
from app.adapters.mediacrawler import SOCIAL_PLATFORM_KEYS, build_social_adapters
from app.adapters.web_fetch import WebFetchAdapter
from app.core.config import Settings, load_settings

_registry: list[SourceAdapter] | None = None


def build_adapters(settings: Settings) -> list[SourceAdapter]:
    """按配置构造适配器列表。

    - Phase 3：bing_cn / web_fetch
    - Phase 4：plugin 组 B 的社媒适配器（默认关闭，可用性由 available() 实时判断，
      所以这里无论如何都构造出来，用户配好 Cookie 后立即可用）
    """
    adapters: list[SourceAdapter] = [BingCnAdapter(settings), WebFetchAdapter(settings)]
    adapters.extend(build_social_adapters(settings))
    return adapters


def get_adapters() -> list[SourceAdapter]:
    """进程内缓存的适配器列表（懒加载配置）。"""
    global _registry
    if _registry is None:
        _registry = build_adapters(load_settings())
    return _registry


def find(name: str) -> SourceAdapter | None:
    """按 name 取适配器。"""
    for adapter in get_adapters():
        if adapter.name == name:
            return adapter
    return None


def available_platforms() -> list[str]:
    """capabilities.platforms：当前可用的源名。"""
    return [adapter.name for adapter in get_adapters() if adapter.available()]


def llm_selectable() -> list[SourceAdapter]:
    """可被模型当作检索源自选的适配器。

    - web_fetch：selectable_by_llm=False 被排除——它只应按用户给出的具体链接调用，
      不能被当成搜索引擎调度
    - 社媒适配器：同为 False——采集耗时长且带风控/封号风险，
      只应由用户请求触发的 search_social_session 显式调度
    """
    return [a for a in get_adapters() if a.available() and a.selectable_by_llm]


def reset() -> None:
    """清空缓存（配置变更或测试时使用）。"""
    global _registry
    _registry = None


def social_adapters() -> list[SourceAdapter]:
    """plugin 组 B 的社媒适配器（含当前不可用者，便于逐条报告原因）。"""
    return [a for a in get_adapters() if a.name in SOCIAL_PLATFORM_KEYS]
