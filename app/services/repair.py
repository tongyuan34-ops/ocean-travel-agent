"""自愈：把「模型说做了却没做」从报告升级成修复。

和 selfcheck 的分工是硬的：
- ``selfcheck`` 只**判断**这一轮是否自洽，产出诊断，不知道重试是什么；
- ``repair``（本模块）只**决策**要不要重跑、重跑时补一句什么话，不执行；
- ``ChatService`` 只**执行**，它不认识具体规则，只认 ``RepairPolicy``。

这么切是为了开闭：以后要修别的失败（比如「说了要查天气却没调工具」），
只需要新增一个 ``RepairRule``，不用改主流程一行代码。

规则拿到的只有 ``Diagnosis`` 这个值对象——用户原话、模型回答、告警码，
不给它 session、graph、事件流。规则想多看一点东西，就得改这个对象，
这样它永远只可能依赖它真正需要的那几个字段。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Diagnosis:
    """一轮对话的体检结论。字段都是快照，规则改不了它们。"""

    code: str
    title: str
    evidence: str
    # 用户这一轮的原话，规则据此决定纠正话术
    user_text: str
    # 模型这一轮的回答，同上
    answer: str


class RepairRule(Protocol):
    """一条自愈规则：认得这个诊断就给一句纠正话术，不认得就返回 None。"""

    def instruction(self, diagnosis: Diagnosis) -> str | None: ...


class RepairPolicy(Protocol):
    """ChatService 依赖的抽象。它不知道底下有几条规则、分别是什么。"""

    def instruction(self, diagnosis: Diagnosis) -> str | None: ...


class ClaimedWithoutActionRule:
    """声称提交了生图任务，但一次工具都没调。

    纠正话术要说三件事：上一轮哪里不对、现在必须做什么、做不到怎么办。
    只写「请重新提交」不够——模型会再写一遍计划，仍然不调工具。
    最后那句「如果确实无法提交就说明原因」是给它留的诚实的退路，
    否则它可能为了满足指令而编一个 task_id 出来。
    """

    def instruction(self, diagnosis: Diagnosis) -> str | None:
        if diagnosis.code != "CLAIMED_WITHOUT_ACTION":
            return None
        return (
            "上一轮你只写了文字说明，没有真正调用 generate_image，"
            "所以没有任何图片在生成，用户会一直等下去。\n"
            "现在请直接调用 generate_image 工具提交任务，不要再描述你打算怎么做。\n"
            "如果确实无法提交（例如工具报错），就直接告诉用户原因，不要声称已经提交。"
        )


class CompositeRepair:
    """按顺序问每条规则，第一条认领的说了算。

    用组合而不是继承：新增规则只是往列表里加一项，
    不会产生「图文自愈子类」「天气自愈子类」这种按失败类型爆炸的继承树。
    """

    def __init__(self, rules: Sequence[RepairRule]) -> None:
        self._rules = tuple(rules)

    def instruction(self, diagnosis: Diagnosis) -> str | None:
        for rule in self._rules:
            text = rule.instruction(diagnosis)
            if text:
                return text
        return None


def build_repair_policy() -> RepairPolicy:
    """默认装配。测试可以传自己的策略，主流程不关心它是什么。"""
    return CompositeRepair([ClaimedWithoutActionRule()])
