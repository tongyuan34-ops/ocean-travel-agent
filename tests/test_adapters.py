"""适配器层：单源失败隔离、降级语义、selectable_by_llm 与结果归一化。

对应 SPEC 4.7（适配器模式、降级机制）与 4.12（单源失败隔离）。
所有用例都不联网：_search 用桩替换。
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from app.adapters import mediacrawler as mc
from app.adapters.base import SourceAdapter
from app.adapters.bing_cn import BingCnAdapter
from app.adapters.registry import build_adapters
from app.adapters.web_fetch import WebFetchAdapter
from app.core.models import SourceKind


class _Stub(SourceAdapter):
    name = "stub"
    kind = SourceKind.GENERAL_CN
    platform = "桩源"

    def __init__(self, settings, *, ok=True, available=True, delay=0.0, exc=None):
        super().__init__(settings)
        self._ok = ok
        self._available = available
        self._delay = delay
        self._exc = exc
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str:
        return "测试桩不可用"

    async def _search(self, query, limit):
        self.calls += 1
        if self._delay:
            import asyncio

            await asyncio.sleep(self._delay)
        if self._exc:
            raise self._exc
        if not self._ok:
            return []
        return [
            mc_doc(self.name, self.platform)
        ]


def mc_doc(source: str, platform: str):
    from app.core.models import Doc, source_kind_value

    return Doc(
        title="结果",
        url="https://example.com/a",
        snippet="摘要",
        source=source,
        source_kind=source_kind_value(SourceKind.GENERAL_CN),
        platform=platform,
    )


# ---------- 降级与隔离 ----------


async def test_unavailable_source_degrades_to_mock(settings):
    adapter = _Stub(dataclasses.replace(settings, enable_mock_fallback=True), available=False)
    docs = await adapter.search("q", 3)
    assert len(docs) == 1
    assert docs[0].mock is True
    assert "测试桩不可用" in docs[0].extras["reason"]
    assert adapter.calls == 0  # 不可用时不发请求


async def test_unavailable_source_skipped_when_fallback_off(settings):
    adapter = _Stub(dataclasses.replace(settings, enable_mock_fallback=False), available=False)
    assert await adapter.search("q", 3) == []


async def test_source_exception_does_not_escape(settings):
    """铁律：任何单源失败都不得拖垮整体流程。"""
    adapter = _Stub(settings, exc=RuntimeError("上游炸了"))
    docs = await adapter.search("q", 3)
    assert len(docs) == 1 and docs[0].mock is True
    assert "RuntimeError" in docs[0].extras["reason"]


async def test_source_timeout_degrades(settings):
    adapter = _Stub(settings, delay=1.0)
    adapter.search_timeout = 0.05
    docs = await adapter.search("q", 3)
    assert docs and docs[0].mock is True
    assert "超时" in docs[0].extras["reason"]


async def test_empty_result_is_not_marked_mock(settings):
    """源正常返回空列表 != 降级，不能误标 mock。"""
    adapter = _Stub(settings, ok=False)
    assert await adapter.search("q", 3) == []


def test_default_timeout_lower_bound(settings):
    """默认超时 = max(source_timeout*3, 45)。"""
    adapter = _Stub(dataclasses.replace(settings, source_timeout_sec=5))
    assert adapter.timeout() == 45.0


# ---------- selectable_by_llm 语义 ----------


def test_registry_marks_fetch_url_not_selectable(settings):
    """fetch_url 入参是具体链接，不能当搜索引擎自选调用。"""
    adapters = {a.name: a for a in build_adapters(settings)}
    assert adapters["web_fetch"].selectable_by_llm is False
    assert adapters["bing_cn"].selectable_by_llm is True


def test_registry_marks_social_not_selectable(settings):
    """社媒采集耗时长且带风控风险，只应由用户请求显式调度。"""
    adapters = {a.name: a for a in build_adapters(settings)}
    for key in mc.SOCIAL_PLATFORM_KEYS:
        assert adapters[key].selectable_by_llm is False, key


def test_registry_contains_all_social_platforms(settings):
    names = {a.name for a in build_adapters(settings)}
    assert set(mc.SOCIAL_PLATFORM_KEYS) <= names


def test_llm_selectable_catalog_excludes_non_selectable(monkeypatch, settings):
    """自选目录（catalog_for_llm）必须排除 fetch_url 与全部社媒源。"""
    from app.adapters import registry

    scoped = dataclasses.replace(
        settings, enable_session_fetch=True, cookie_dir=str(settings.cookie_dir)
    )
    monkeypatch.setattr(registry, "load_settings", lambda: scoped)
    registry.reset()
    try:
        names = {a.name for a in registry.llm_selectable()}
    finally:
        registry.reset()

    assert "bing_cn" in names
    assert "web_fetch" not in names
    assert not (names & set(mc.SOCIAL_PLATFORM_KEYS))


# ---------- Bing ----------


def test_bing_available_without_key(settings):
    """无 Key 也要可用：回退 HTML 抓取是它的既定路线。"""
    adapter = BingCnAdapter(dataclasses.replace(settings, bing_search_api_key=""))
    assert adapter.available() is True


def test_bing_flags_verification_page():
    from app.adapters.bing_cn import _is_verify_page

    assert _is_verify_page("<html>Please verify you are human</html>") is True
    assert _is_verify_page("<html><li class='b_algo'>ok</li></html>") is False


def test_bing_parses_html_results():
    from app.adapters.bing_cn import _parse_results

    html = """
    <ul>
      <li class="b_algo"><h2><a href="https://a.example/1">标题一</a></h2>
          <p>摘要一</p></li>
      <li class="b_algo"><h2><a href="https://b.example/2">标题二</a></h2>
          <p>摘要二</p></li>
    </ul>
    """
    docs = _parse_results(html, 5, "bing_cn", SourceKind.GENERAL_CN, "必应")
    assert len(docs) == 2
    assert docs[0].url == "https://a.example/1"
    assert docs[1].snippet == "摘要二"
    assert docs[0].source == "bing_cn"


def test_bing_skips_non_http_links():
    from app.adapters.bing_cn import _parse_results

    html = '<li class="b_algo"><h2><a href="/relative">相对链接</a></h2></li>'
    assert _parse_results(html, 5, "bing_cn", SourceKind.GENERAL_CN, "必应") == []


# ---------- 社媒结果归一化 ----------


def _write(tmp_path, code, name, payload, *, mtime=None):
    base = tmp_path / code / "json"
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    import json

    path.write_text(
        json.dumps(payload, ensure_ascii=False) if not name.endswith(".jsonl")
        else "\n".join(json.dumps(i, ensure_ascii=False) for i in payload),
        encoding="utf-8",
    )
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


def test_collect_docs_filters_by_mtime(tmp_path):
    """旧文件必须被过滤，否则会把上次甚至未登录时的结果混进来。"""
    recent = [
        {"aweme_id": "1", "title": "新", "desc": "新内容", "digg_count": 5},
    ]
    _write(tmp_path, "douyin", "search_contents_new.json", recent)
    _write(tmp_path, "douyin", "search_contents_old.json", [{"aweme_id": "9", "title": "旧"}], mtime=0)

    docs = mc.collect_docs(
        platform_key="douyin", label="抖音", code="douyin",
        data_dir=tmp_path, since=time.time() - 5, limit=10,
    )
    assert [d.title for d in docs] == ["新"]


def test_collect_docs_handles_both_directory_names(tmp_path):
    """各平台落盘目录名不统一（bili / bilibili），两个候选都要试。"""
    _write(tmp_path, "bili", "search_contents_a.json", [{"video_id": "BV1x", "title": "B站"}])
    docs = mc.collect_docs(
        platform_key="bilibili", label="B站", code="bili",
        data_dir=tmp_path, since=0, limit=10,
    )
    assert len(docs) == 1 and docs[0].title == "B站"


def test_collect_docs_dedupes_across_dirs(tmp_path):
    payload = [{"aweme_id": "1", "title": "重复"}]
    _write(tmp_path, "dy", "search_contents_x.json", payload)
    _write(tmp_path, "douyin", "search_contents_y.json", payload)
    docs = mc.collect_docs(
        platform_key="douyin", label="抖音", code="dy",
        data_dir=tmp_path, since=0, limit=10,
    )
    assert len(docs) == 1


def test_hot_comments_sorted_deduped_top3(tmp_path):
    contents = [{"aweme_id": "1", "title": "t"}]
    comments = [
        {"aweme_id": "1", "content": "A", "nickname": "n1", "digg_count": 10},
        {"aweme_id": "1", "content": "A", "nickname": "n1", "digg_count": 10},  # 重复
        {"aweme_id": "1", "content": "B", "nickname": "n2", "digg_count": 50},
        {"aweme_id": "1", "content": "C", "nickname": "n3", "digg_count": 30},
        {"aweme_id": "1", "content": "D", "nickname": "n4", "digg_count": 1},
    ]
    _write(tmp_path, "douyin", "search_contents_a.json", contents)
    _write(tmp_path, "douyin", "search_comments_a.json", comments)

    docs = mc.collect_docs(
        platform_key="douyin", label="抖音", code="douyin",
        data_dir=tmp_path, since=0, limit=10,
    )
    hot = docs[0].extras["hot_comments"]
    assert [c["content"] for c in hot] == ["B", "C", "A"]
    assert [c["likes"] for c in hot] == [50, 30, 10]


def test_bilibili_numeric_id_gets_av_prefix(tmp_path):
    _write(tmp_path, "bili", "search_contents_a.json", [{"video_id": "123456", "title": "x"}])
    docs = mc.collect_docs(
        platform_key="bilibili", label="B站", code="bili",
        data_dir=tmp_path, since=0, limit=10,
    )
    assert docs[0].url.endswith("/av123456")


def test_missing_cookie_refuses_to_run(settings):
    """带未登录态硬跑只会返回空结果，必须在适配器层就拒绝。"""
    import asyncio

    scoped = dataclasses.replace(
        settings, enable_session_fetch=True, cookie_dir=str(settings.cookie_dir) + "-absent"
    )
    adapter = mc.MediaCrawlerAdapter(scoped, "douyin")
    with pytest.raises(RuntimeError, match="Cookie"):
        asyncio.run(adapter._search("q", 1))


def test_unavailable_reasons_are_specific(settings):
    scoped = dataclasses.replace(settings, enable_session_fetch=False)
    reason = mc.MediaCrawlerAdapter(scoped, "douyin").unavailable_reason()
    assert "ENABLE_SESSION_FETCH" in reason


def test_subprocess_env_uses_playwright_node_when_no_system_node(
    settings, monkeypatch, tmp_path
):
    """系统无 node 时挂上 Playwright 自带的 node，否则 PyExecJS 会退回 JScript。"""
    adapter = mc.MediaCrawlerAdapter(settings, "douyin")
    monkeypatch.setattr(mc.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mc.MediaCrawlerAdapter, "_node_dir", lambda self: tmp_path)
    (tmp_path / "node.exe").write_text("", encoding="utf-8")

    env = adapter._subprocess_env()
    assert env is not None
    assert env["EXECJS_RUNTIME"] == "Node"
    assert str(tmp_path) in env["PATH"]


def test_subprocess_env_respects_existing_node(settings, monkeypatch):
    """系统已有 node 时不要覆盖用户环境。"""
    adapter = mc.MediaCrawlerAdapter(settings, "douyin")
    monkeypatch.setattr(mc.shutil, "which", lambda _name: r"C:\nodejs\node.exe")
    assert adapter._subprocess_env() is None


# ---------- WebFetch ----------


def test_web_fetch_extracts_opengraph():
    from app.adapters.web_fetch import extract_meta, extract_readable

    html = """
    <html><head><title>原标题</title>
    <meta property="og:title" content="分享标题">
    <meta property="og:description" content="文案 地点：亚龙湾 #三亚#">
    <meta property="og:image" content="https://img.example.com/a.jpg">
    </head><body><script>var leak=1</script><p>正文</p></body></html>
    """
    meta = extract_meta(html)
    assert meta["og:title"] == "分享标题"
    assert "亚龙湾" in meta["og:description"]
    title, text = extract_readable(html, "text/html")
    assert title == "原标题"
    assert "leak" not in text
    assert "正文" in text


def test_web_fetch_available_always(settings):
    assert WebFetchAdapter(settings).available() is True


def test_place_hints_are_marked_as_clues():
    """分享文案里的地名只能当线索，不得当既定事实。"""
    from app.agent.tools.plugin import _place_hints, _tags

    hints = _place_hints("地点：亚龙湾 #三亚旅行# 去了蜈支洲岛")
    assert any("亚龙湾" in h for h in hints)
    assert "三亚旅行" in _tags("#三亚旅行#")
