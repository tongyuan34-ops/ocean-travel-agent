"""技能系统：加载、触发、提示词、激活与 HTTP 契约。

这里刻意分两类：

- 用临时目录造技能的用例，钉住「格式怎么解析、坏技能怎么跳过」；
- 用仓库自带技能的用例，钉住「四个技能真的能被加载、能命中触发词」——
  后者能挡住「改了目录结构但没人发现」这类静默失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.tools.skills import use_skill
from app.services.sessions import SessionService
from app.skills import (
    activate_skills,
    bind_active_skills,
    catalog_block,
    get_skill,
    instructions_block,
    load_skill,
    load_skills,
    match_skills,
    skills,
    split_command,
    tools_for_skills,
)
from app.storage import MemorySessionStore


@pytest.fixture(autouse=True)
def _skill_root(monkeypatch):
    """所有用例都盯住仓库自带的 skills/。

    注册表按根目录缓存，而根目录来自 ``SKILLS_DIR``；不显式指定的话，
    开发机 .env 里一个不同的值就能让这批用例失败在完全无关的地方。
    """
    monkeypatch.setenv("SKILLS_DIR", str(Path(__file__).resolve().parents[1] / "skills"))
    yield
    bind_active_skills([])
    skills(refresh=True)


_TOOL_SOURCE = '''
from langchain_core.tools import tool


@tool
def ping(text: str = "") -> str:
    """测试用工具：原样回显。"""
    return "pong:" + text


TOOLS = [ping]
'''


def _make_skill(root: Path, key: str, body: str = "", tools: bool = False) -> Path:
    folder = root / key
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\n"
        f"name: {key} 技能\n"
        f"description: {key} 的描述\n"
        "triggers: 测试, trig\n"
        "---\n\n"
        f"{body or '# 步骤' + chr(10) + chr(10) + '1. 做点什么。'}\n",
        encoding="utf-8",
    )
    if tools:
        (folder / "tools.py").write_text(_TOOL_SOURCE, encoding="utf-8")
    return folder


# ---------- 加载 ----------


def test_load_skill_parses_frontmatter_and_body(tmp_path: Path):
    _make_skill(tmp_path, "demo", body="# 步骤\n\n1. 先查再答。")

    skill = load_skill(tmp_path / "demo")

    assert skill is not None
    assert (skill.key, skill.name) == ("demo", "demo 技能")
    assert skill.description == "demo 的描述"
    assert skill.triggers == ("测试", "trig")
    assert "先查再答" in skill.instructions


def test_skill_tools_are_namespaced(tmp_path: Path):
    _make_skill(tmp_path, "demo", tools=True)

    skill = load_skill(tmp_path / "demo")

    assert skill is not None
    assert [tool.name for tool in skill.tools] == ["skill__demo__ping"]


def test_broken_tool_module_keeps_prompt_and_records_error(tmp_path: Path):
    folder = _make_skill(tmp_path, "demo", tools=True)
    (folder / "tools.py").write_text("raise RuntimeError('boom')", encoding="utf-8")

    skill = load_skill(folder)

    assert skill is not None
    assert skill.tools == ()
    assert "boom" in skill.error
    # 提示词仍然可用：工具坏了只是少一个执行入口，不该让整个技能消失
    assert skill.instructions


def test_missing_fields_and_bad_key_are_skipped(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: 只有名字\n---\n\n正文", encoding="utf-8")

    chinese = tmp_path / "中文目录"
    chinese.mkdir()
    (chinese / "SKILL.md").write_text(
        "---\nname: 中文\ndescription: 非法 key\n---\n\n正文", encoding="utf-8"
    )

    found = load_skills(tmp_path)

    assert found == {}


def test_templates_are_collected(tmp_path: Path):
    folder = _make_skill(tmp_path, "demo")
    templates = folder / "templates"
    templates.mkdir()
    (templates / "table.md").write_text("| a | b |", encoding="utf-8")
    (templates / "blob.bin").write_bytes(b"\x00\x01")

    skill = load_skill(folder)

    assert skill is not None
    assert [path.name for path in skill.templates] == ["table.md"]


# ---------- 仓库自带技能 ----------


def test_shipped_skills_are_loadable():
    keys = [item.key for item in skills()]

    assert {"budget-split", "trip-check", "destination-compare", "packing-list"} <= set(keys)


def test_packing_list_ships_a_tool():
    item = get_skill("packing-list")

    assert item is not None
    assert item.has_tools
    assert item.tools[0].name.startswith("skill__packing-list__")
    # 工具确实能生成清单，而不是只有名字
    rendered = item.tools[0].invoke({"destination": "三亚", "days": 2, "companions": ["小孩"]})
    assert "三亚" in rendered and "带小孩追加" in rendered


def test_trigger_matching_prefers_real_phrasings():
    assert match_skills("帮我做个预算表，两个人") == ["budget-split"]
    assert "packing-list" in match_skills("去三亚要带什么")
    assert "trip-check" in match_skills("帮我看看行程会不会太赶")


def test_catalog_is_compact_and_instructions_carry_template():
    catalog = catalog_block()
    assert "预算分项" in catalog
    # 清单只给名字和一句话，不把正文整段塞进来
    assert "## 步骤" not in catalog
    assert "| 维度 |" not in catalog

    body = instructions_block("destination-compare")
    assert "目的地对比" in body
    # 纯提示词技能靠内联模板才知道表头长什么样
    assert "| 维度 |" in body


def test_tools_for_skills_only_returns_selected():
    assert [tool.name for tool in tools_for_skills(["packing-list"])] == [
        "skill__packing-list__build"
    ]
    assert tools_for_skills(["budget-split"]) == []


# ---------- 命令与激活 ----------


def test_slash_command_is_split_only_for_known_skills():
    assert split_command("/packing-list 去三亚 4 天") == ("packing-list", "去三亚 4 天")
    assert split_command("/skill trip-check") == ("trip-check", "")
    # 未知 key 不吞用户输入：它可能只是普通文本
    assert split_command("/tmp 里有什么") == ("", "/tmp 里有什么")


def test_activate_skills_reports_only_new_keys():
    bind_active_skills(["budget-split"])

    assert activate_skills(["budget-split"]) == []
    assert activate_skills(["trip-check"]) == ["trip-check"]

    bind_active_skills([])


def test_use_skill_lists_when_empty_and_rejects_unknown():
    bind_active_skills([])

    listing = use_skill.invoke({"skill": ""})
    assert "预算分项" in listing

    missing = use_skill.invoke({"skill": "not-exist"})
    assert missing.startswith("SKILL_NOT_FOUND")

    loaded = use_skill.invoke({"skill": "packing-list", "task": "去三亚"})
    assert "行前清单" in loaded and "去三亚" in loaded
    bind_active_skills([])


# ---------- HTTP ----------


def test_skills_endpoint_lists_without_instructions(client):
    data = client.get("/api/v1/skills").json()

    assert data["enabled"] is True
    keys = {item["key"] for item in data["items"]}
    assert {"budget-split", "packing-list"} <= keys
    # 正文是内部实现，不能通过接口泄露
    assert all("instructions" not in item for item in data["items"])


def test_session_skill_can_be_set_and_cleared(client):
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]

    body = client.patch(f"/api/v1/chat/sessions/{session_id}/skill", json={"skill": "trip-check"})
    assert body.status_code == 200
    assert body.json()["skill"] == "trip-check"
    assert client.get(f"/api/v1/chat/sessions/{session_id}/messages").json()["skill"] == (
        "trip-check"
    )

    cleared = client.patch(f"/api/v1/chat/sessions/{session_id}/skill", json={"skill": ""})
    assert cleared.json()["skill"] == ""


def test_unknown_skill_is_rejected(client):
    session_id = client.post("/api/v1/chat/sessions").json()["session_id"]

    response = client.patch(
        f"/api/v1/chat/sessions/{session_id}/skill", json={"skill": "not-exist"}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("name", ["skill__packing-list__build"])
def test_skill_tool_category(name: str):
    from app.services.turn_events import tool_category

    assert tool_category(name) == "skill"


# ---------- 接入对话流程 ----------


class _FakeGraph:
    """只回一条最终消息的图，用来检查「送进去的提示词和工具对不对」。"""

    def __init__(self, captured: dict) -> None:
        self._captured = captured

    async def astream_events(self, _input, version=None, config=None):
        from langchain_core.messages import AIMessage

        self._captured["messages"] = list(_input.get("messages") or [])
        yield {"event": "on_chain_end", "data": {"output": {"messages": [AIMessage(content="好")]}}}


def _fake_service(monkeypatch) -> tuple[object, dict]:
    from app.core.config import load_settings
    from app.services import chat as chat_module

    captured: dict = {}

    def fake_build(settings, mode=None, tools=None, model=None):
        captured["tools"] = [tool.name for tool in tools or []]
        return _FakeGraph(captured), "native"

    monkeypatch.setattr(chat_module, "build_agent_graph", fake_build)
    return chat_module.ChatService(load_settings(), SessionService(MemorySessionStore())), captured


async def test_explicit_skill_injects_instructions_and_binds_tool(monkeypatch):
    service, captured = _fake_service(monkeypatch)
    session = await service._sessions.create()

    async for _frame in service.stream_reply(session.id, "/packing-list 去三亚 3 天"):
        pass

    system = captured["messages"][0].content
    assert "行前清单" in system
    assert "skill__packing-list__build" in system
    # 工具列表里也要有它，否则提示词让模型调用时手上没有
    assert "skill__packing-list__build" in captured["tools"]
    # 命令本身不落库：历史里应该是用户真正说的那句话
    stored = await service._sessions.get(session.id)
    assert stored.messages[0].content == "去三亚 3 天"


async def test_sticky_skill_applies_without_command(monkeypatch):
    service, captured = _fake_service(monkeypatch)
    session = await service._sessions.create()
    await service._sessions.set_skill(session.id, "trip-check")

    async for _frame in service.stream_reply(session.id, "帮我看看这份行程"):
        pass

    system = captured["messages"][0].content
    assert "行程体检" in system
    stored = await service._sessions.get(session.id)
    assert stored.skill == "trip-check"
