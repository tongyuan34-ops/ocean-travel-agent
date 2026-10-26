"""对话风格与系统提示词组装。

核心断言只有一条：**闲聊型人格拿不到旅行规则**。

这不是洁癖。实测（`scripts/ab_persona.py`，每臂 3 次）在旅行提示词里加一句
「别提旅行」只能降低概率——同一份提示词连跑两次，一次 2/2 通过、一次 0/2，
模型改用「三亚」「想去的地方」绕过了关键词黑名单。只要规则里还有天气、路线、
行程这些词，它就有东西可拐；把这些规则整块拿掉，才是确定性的。
"""

from __future__ import annotations

from app.agent.prompts import (
    CORE_RULES,
    DEFAULT_PERSONA,
    PERSONA_KEYS,
    PERSONAS,
    TRAVEL_RULES,
    build_system_prompt,
    get_persona,
    persona_list,
)

# 旅行味道最重的几个词：闲聊型人格的提示词里一个都不该出现
TRAVEL_WORDS = ("天气", "路线", "行程", "景点", "POI", "plan_route", "search_poi")


# ---------- 风格表本身 ----------


def test_four_personas_with_unique_keys():
    assert len(PERSONAS) == 4
    assert len(set(PERSONA_KEYS)) == 4
    assert DEFAULT_PERSONA == "concierge"


def test_persona_keys_are_lowercase_ascii():
    """key 会出现在 URL 和会话存储里，中文或大写只会带来麻烦。"""
    for key in PERSONA_KEYS:
        assert key.isascii() and key.islower()


def test_every_persona_has_display_fields():
    for item in PERSONAS:
        assert item.name and item.latin and item.tagline
        assert item.style, f"{item.key} 没有说话方式，等于选了没效果"


def test_persona_list_does_not_leak_prompts():
    """接口只给展示字段。prompt 正文是内部实现，暴露出去等于把调参过程开源。"""
    for item in persona_list():
        assert set(item) == {"key", "name", "latin", "tagline"}


# ---------- 回落 ----------


def test_unknown_persona_falls_back_to_default():
    """老会话可能带着已经被删掉的 key。这时候整轮 500 比悄悄用默认风格更糟。"""
    assert get_persona("nonexistent").key == DEFAULT_PERSONA
    assert get_persona("").key == DEFAULT_PERSONA
    assert get_persona(None).key == DEFAULT_PERSONA


def test_persona_key_is_case_and_space_insensitive():
    assert get_persona("  Confidant ").key == "confidant"


# ---------- 组装 ----------


def test_core_rules_have_no_job_title():
    """身份写成「你是 X 助手」，用户要求换人格时就变成一场身份冲突，
    模型只能折中：保留主业骨架、刷一层温柔语气。"""
    assert "你是旅行规划助手" not in CORE_RULES
    assert "旅行规划助手" not in CORE_RULES


def test_core_rules_do_not_demand_lists():
    """「条理清晰」四个字是列表的开关：模型对它的默认实现就是分点，
    于是「我好迷茫」被拆成 A/B/C 三类，读起来像问卷。"""
    assert "条理清晰" not in CORE_RULES


def test_confidant_gets_no_travel_rules():
    prompt = build_system_prompt("confidant")
    leaked = [word for word in TRAVEL_WORDS if word in prompt]
    assert not leaked, f"闲聊人格的提示词里还有旅行规则：{leaked}"
    assert TRAVEL_RULES not in prompt


def test_travel_personas_get_travel_rules():
    """反向约束：不能为了「别推行程」把旅行能力一起删了。"""
    for key in ("concierge", "companion", "guide"):
        prompt = build_system_prompt(key)
        assert TRAVEL_RULES in prompt, f"{key} 应该带领域规则"


def test_all_personas_keep_safety_rules():
    """安全规则（注入防护、不编造、权限确认）属于核心，任何风格都不能丢。"""
    for key in PERSONA_KEYS:
        prompt = build_system_prompt(key)
        assert "untrusted" in prompt
        assert "TOOL_BLOCKED_BY_GUARD" in prompt
        assert "严禁凭记忆编造" in prompt


def test_style_section_is_last():
    """说话方式放最后：它最具体、离生成最近，放前面会被规则淹没。"""
    prompt = build_system_prompt("companion")
    assert prompt.endswith(get_persona("companion").style)


def test_personas_produce_different_prompts():
    prompts = {build_system_prompt(key) for key in PERSONA_KEYS}
    assert len(prompts) == 4
