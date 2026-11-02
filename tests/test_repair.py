"""自愈决策层：只判断「要不要重跑、补什么话」，不执行、不碰图。

这些用例不碰 ChatService，也就不会联网、不会起图——
决策逻辑可以单独想清楚，执行逻辑另有一套用例管。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.repair import (
    ClaimedWithoutActionRule,
    CompositeRepair,
    Diagnosis,
    build_repair_policy,
)


def _diag(code: str = "CLAIMED_WITHOUT_ACTION") -> Diagnosis:
    return Diagnosis(
        code=code,
        title="这一轮没有真的提交生图任务",
        evidence="用户要图 · 回答声称完成 · 工具调用 0 次",
        user_text="帮我生成三张旅行图",
        answer="三张图都提交生成好了。",
    )


def test_claimed_without_action_rule_speaks_three_things():
    """纠正话术必须同时说清：哪错了、现在做什么、做不到怎么办。

    少了最后一句，模型可能为了「完成任务」编一个 task_id 出来——
    那比不干活更糟，用户会去轮询一个不存在的任务。
    """
    text = ClaimedWithoutActionRule().instruction(_diag())

    assert text is not None
    assert "没有真正调用 generate_image" in text
    assert "直接调用 generate_image" in text
    assert "如果确实无法提交" in text


def test_rule_declines_other_diagnoses():
    """规则只认自己那一种，别的告警不能乱接。"""
    assert ClaimedWithoutActionRule().instruction(_diag("TOOL_FAILED_SILENTLY")) is None


def test_composite_returns_first_claim():
    """多条规则按顺序问，第一条认领的说了算，后面的不再执行。"""

    class Never:
        def instruction(self, diagnosis: Diagnosis) -> str | None:
            return None

    class Second:
        def __init__(self) -> None:
            self.calls = 0

        def instruction(self, diagnosis: Diagnosis) -> str | None:
            self.calls += 1
            return "第二条"

    second = Second()
    policy = CompositeRepair([Never(), ClaimedWithoutActionRule(), second])

    assert policy.instruction(_diag()) == ClaimedWithoutActionRule().instruction(_diag())
    assert second.calls == 0


def test_composite_with_no_matching_rule_declines():
    """没人认领就返回 None，让上层照旧把问题摆给用户，不硬修。"""
    policy = CompositeRepair([])

    assert policy.instruction(_diag()) is None


def test_default_policy_handles_the_real_failure():
    """默认装配必须能处理真实撞到的那一类，否则等于没接。"""
    assert build_repair_policy().instruction(_diag()) is not None


def test_diagnosis_is_frozen():
    """规则拿到的是快照。能改就能把证据改掉，事后没法复盘。"""
    diag = _diag()
    with pytest.raises(dataclasses.FrozenInstanceError):
        diag.code = "OK"  # type: ignore[misc]
