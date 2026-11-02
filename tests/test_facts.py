"""每轮补进上下文的事实块：只拼文字，不查库。

这些用例不需要 Redis、不需要生图服务——采集与查证由 chat.py 做，
这里只钉住「事实写成了什么话」和「没有事实时一个字都不占」。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.facts import (
    MediaFact,
    WarningFact,
    build_turn_facts,
    last_warnings,
    recent_task_ids,
)


@dataclass
class _Msg:
    role: str
    media_task_ids: list[str] | None = None
    warnings: list[dict[str, str]] | None = None


def test_no_facts_returns_empty_string():
    """没有任何事实时不能返回一个空壳标题——那是白花的 token。"""
    assert build_turn_facts([], []) == ""


def test_successful_task_is_stated_as_success():
    """核心用例：任务成功必须说成成功，并明确指向「展示环节」。"""
    text = build_turn_facts([MediaFact("81fcd01d-1111", "succeeded")], [])

    assert "81fcd01d" in text
    assert "已成功生成" in text
    assert "问题在展示环节" in text
    # 必须禁止它把「看不到图」当成服务故障
    assert "不要宣布服务故障" in text


def test_failed_task_carries_the_reason():
    """失败要带原因，否则模型只能猜，猜出来的话用户会当真。"""
    text = build_turn_facts(
        [MediaFact("aaaa1111-2222", "failed", "生图账号欠费，图片没有生成")], []
    )

    assert "失败了" in text
    assert "欠费" in text


def test_running_task_is_not_reported_as_done():
    """还在跑的任务不能说成完成，否则用户去刷新也看不到图，又是一轮空等。"""
    text = build_turn_facts([MediaFact("bbbb1111-2222", "running")], [])

    assert "还在生成中" in text
    assert "已成功生成" not in text


def test_warning_tells_the_model_its_own_words_were_not_fact():
    """上一轮的告警必须说清「那句话不是事实」。

    历史里只喂 role 和 content，模型会把自己编的「已提交」当事实读回去，
    越推越偏。这段文字是唯一能打断这条链的东西。
    """
    text = build_turn_facts(
        [], [WarningFact("CLAIMED_WITHOUT_ACTION", "这一轮没有真的提交生图任务")]
    )

    assert "CLAIMED_WITHOUT_ACTION" in text
    assert "不是事实" in text
    assert "不要拿它当作推断服务状态的依据" in text


def test_long_error_is_truncated():
    """完整报错在任务卡片上，上下文里只留够判断的那截。"""
    text = build_turn_facts([MediaFact("cccc1111-2222", "failed", "错" * 500)], [])

    assert "…" in text
    assert len(text) < 400


def test_recent_task_ids_dedups_and_keeps_newest_first():
    msgs = [
        _Msg("assistant", ["a" * 8, "b" * 8]),
        _Msg("assistant", ["b" * 8, "c" * 8]),
    ]

    assert recent_task_ids(msgs) == ["c" * 8, "b" * 8, "a" * 8]


def test_recent_task_ids_is_capped():
    """不封顶的话，长会话每轮都要为几十个旧任务付 token。"""
    msgs = [_Msg("assistant", [f"id{i:06d}" for i in range(20)])]

    assert len(recent_task_ids(msgs, limit=6)) == 6


def test_recent_task_ids_ignores_messages_without_tasks():
    msgs = [_Msg("user"), _Msg("assistant", ["x" * 8]), _Msg("user")]

    assert recent_task_ids(msgs) == ["x" * 8]


def test_last_warnings_reads_the_last_assistant_message():
    """用户消息上没有告警，必须跳过才找得到真正要说的那一条。"""
    msgs = [
        _Msg("assistant", warnings=[{"code": "OLD", "title": "旧"}]),
        _Msg("user"),
        _Msg("assistant", warnings=[{"code": "NEW", "title": "新"}]),
        _Msg("user"),
    ]

    got = last_warnings(msgs)

    assert [w.code for w in got] == ["NEW"]
    assert got[0].title == "新"


def test_last_warnings_empty_when_last_assistant_was_clean():
    """上一轮修好了就不该再提旧账，否则模型会一直以为自己还在犯错。"""
    msgs = [
        _Msg("assistant", warnings=[{"code": "OLD", "title": "旧"}]),
        _Msg("assistant", warnings=[]),
        _Msg("user"),
    ]

    assert last_warnings(msgs) == []
