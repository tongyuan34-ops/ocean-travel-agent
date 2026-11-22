"""工具路由：按本轮话题挑工具子集。

这一层存在的唯一理由是**省钱和省延迟**：32 个工具的 schema 约 13k token，
而 ``bind_tools`` 是每轮都要重发一遍的。所以这里的断言分两类：

- 行为类：什么话该绑哪几组，追问会不会掉工具，闲聊人格会不会拿到旅行工具；
- 预算类：一句「你好」到底还要付多少工具钱。这一条最容易被后续改动悄悄破坏
  （加一个工具、把某个触发词写宽一点），所以用数字钉住。

关键词路由天然有漏网的时候，测试不追求「每句都对」，而是钉住**已知的真实说法**
——它们来自实际对话里出现过的句子，不是编的。
"""

from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool

from app.agent import toolset
from app.agent.prompts import build_system_prompt
from app.agent.tools import ALL_TOOLS
from app.agent.tools.meta import discover_tools
from app.skills import catalog_block


def _names(tools: list[BaseTool]) -> set[str]:
    return {tool.name for tool in tools}


def _pick(text: str, **kwargs: object) -> set[str]:
    return _names(toolset.select_tools(text, pool=ALL_TOOLS, **kwargs))  # type: ignore[arg-type]


# ---------- 闲聊：不该带任何业务工具 ----------


@pytest.mark.parametrize("text", ["你好", "谢谢！", "在吗", "嗯嗯"])
def test_small_talk_binds_only_the_meta_tool(text: str):
    """问候、道谢不是任务。这时候绑 32 个工具，模型既不会用也读不懂。

    常驻的只有元工具：申请工具组（discover_tools）与取技能（use_skill），
    两者都只返回说明，不产生副作用。
    """
    assert _pick(text) == set(toolset.META_TOOLS)


def test_meta_tool_is_always_present():
    """没有它，关键词漏网时模型只能回答「我做不到」。"""
    for text in ("你好", "帮我规划三亚 4 天", "把这个表按金额排一下"):
        assert toolset.META_TOOL in _pick(text)


# ---------- 真实说法 → 组 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("帮我规划三亚 4 天亲子行程", "trip"),
        ("杭州三天怎么玩", "trip"),
        ("那第二天呢", "trip"),
        ("三亚住哪比较方便，预算 600 一晚", "stay"),
        ("从北京到三亚怎么去", "stay"),
        ("帮我看看这个表按金额排一下", "docs"),
        ("这些数据一共多少钱", "docs"),
        ("帮我把这个归类一下", "docs"),
        ("把这个文件夹打开", "file"),
        ("搜一下西湖的开放时间", "web"),
        ("小红书上有哪些三亚避坑经验", "social"),
        ("画一张海边打卡照", "media"),
    ],
)
def test_real_phrasings_hit_the_expected_group(text: str, expected: str):
    keys = toolset.route(text)
    assert expected in keys, f"「{text}」没命中 {expected}，实际 {keys}"


def test_numeric_day_pattern_does_not_match_today():
    """``\\d+天`` 这条正则存在的原因：写成子串「天」会误伤「今天」。"""
    assert "trip" not in toolset.route("今天好累")
    assert "trip" in toolset.route("三亚 4 天")


# ---------- 追问沿用 ----------


def test_follow_up_keeps_previous_groups():
    """「那第二天呢」一个触发词都不沾，靠上一轮的组续命。"""
    assert toolset.route("那第二天呢", active=["trip"]) == ["trip"]
    assert "plan_trip" in _pick("那第二天呢", active=["trip"])


def test_switching_topic_drops_stale_groups():
    """用户从表格转到行程，还留着 docs 组只是白花钱。"""
    assert toolset.route("帮我规划三亚 4 天", active=["docs"]) == ["trip"]


def test_unknown_active_groups_are_ignored():
    """会话里可能留着已经被删掉的组名（老版本存的），不能因此抛异常。"""
    assert toolset.route("你好", active=["nope", "gone"]) == []


# ---------- 附件与人格 ----------


def test_attachment_extension_activates_docs():
    """用户传了 xlsx 只说「看看」，关键词一个都不沾，但显然要用 docs 组。"""
    assert "docs" in toolset.route("看看这个", attachments=["7月下半旬.xlsx"])
    assert "docs" in toolset.route("看看这个", attachments=["合同.DOCX"])


def test_confidant_gets_no_group_tools():
    """闲聊人格的设计意图是「只陪聊」，拿到旅行工具就会被描述词拽回主业。"""
    assert toolset.route("帮我规划三亚 4 天", persona="confidant") == []


# ---------- 上限与逃生舱 ----------


def test_tool_count_never_exceeds_the_cap():
    everything = " ".join(word for group in toolset.GROUPS for word in group.triggers)
    tools = toolset.select_tools(everything, pool=ALL_TOOLS)
    # +常驻元工具（discover_tools / use_skill）：它们不进任何组，但每轮都绑
    assert len(tools) <= toolset.MAX_TOOLS + len(toolset.META_TOOLS)


def test_routing_all_restores_full_binding():
    """排查问题时要能一键退回旧行为。"""
    tools = toolset.select_tools("你好", pool=ALL_TOOLS, routing="all")
    assert _names(tools) == {tool.name for tool in ALL_TOOLS}


def test_tool_order_follows_the_pool():
    """顺序必须稳定：图缓存拿工具名集合当 key，顺序抖动会让缓存一直失效。"""
    first = [t.name for t in toolset.select_tools("帮我规划三亚 4 天", pool=ALL_TOOLS)]
    second = [t.name for t in toolset.select_tools("三亚行程怎么安排", pool=ALL_TOOLS)]
    assert first == second
    assert first == [tool.name for tool in ALL_TOOLS if tool.name in set(first)]


# ---------- discover_tools ----------


def test_discover_tools_activates_groups_for_the_next_turn():
    box = toolset.bind_active([])
    result = discover_tools.invoke({"need": "把桌面上的表格按金额排序"})
    assert "docs" in box, "没有把命中的组写回上下文，下一轮还是没工具"
    assert "已激活" in result


def test_discover_tools_without_need_only_lists():
    box = toolset.bind_active(["trip"])
    result = discover_tools.invoke({"need": ""})
    assert box == {"trip"}
    assert "行程规划" in result


def test_discover_tools_reports_when_nothing_matches():
    box = toolset.bind_active([])
    result = discover_tools.invoke({"need": "随便聊聊"})
    assert box == set()
    assert "没有匹配到" in result


# ---------- 预算：这条是本次重构的目的 ----------


def test_small_talk_input_stays_under_budget():
    """一句「你好」的固定输入（提示词 + 工具 schema）。

    重构前是 16,233 token（提示词 3,089 + 32 个工具 13,144）。这里留了余量：
    2,800 以内都算通过——再涨回四位数就说明工具又变成全量绑定了。

    上一版是 2,200。多出来的部分：第二个常驻元工具 ``use_skill`` 约 250 token，
    技能清单（每个技能一行）约 230 token。两者都是「模型自主用技能」的必要成本，
    但仍然只有全量绑定时的六分之一。
    """
    tiktoken = pytest.importorskip("tiktoken")
    encoding = tiktoken.get_encoding("cl100k_base")

    def count(text: str) -> int:
        return len(encoding.encode(text))

    # 真实一轮的 system prompt = 人格提示词 + 技能清单（ChatService 里拼上去）
    prompt = count(build_system_prompt("concierge")) + count(catalog_block())
    tools = 0
    for tool in toolset.select_tools("你好", pool=ALL_TOOLS):
        schema = tool.tool_call_schema.model_json_schema()  # type: ignore[attr-defined]
        tools += count(tool.name) + count(tool.description or "") + count(str(schema))

    total = prompt + tools
    assert total < 2800, (
        f"「你好」的固定输入涨到 {total} token 了（提示词 {prompt} + 工具 {tools}）"
    )
