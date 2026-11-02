"""本轮自检：模型说「已提交」但没调工具，必须被识别出来。

这一组用例守的是一个具体的故障：用户要三张图，模型连续四轮说
「已经提交」，而 `generate_image` 一次都没被调用，页面上永远没有图。
"""

from __future__ import annotations

import pytest

from app.services.selfcheck import check_turn


def _run(
    *,
    user_text: str = "帮我生成三张旅行图",
    answer: str = "三张图都提交生成好了，等前端轮询出图就会显示。",
    tool_names: tuple[str, ...] = (),
    media_task_ids: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    return check_turn(
        user_text=user_text,
        answer=answer,
        tool_names=tool_names,
        media_task_ids=media_task_ids,
    )


def test_claim_without_tool_call_is_reported():
    warnings = _run()
    assert len(warnings) == 1
    assert warnings[0]["code"] == "CLAIMED_WITHOUT_ACTION"
    # 告警必须说清楚"没有图片在生成"，而不是含糊地"可能有问题"
    assert "没有" in warnings[0]["message"]
    assert warnings[0]["title"]


def test_evidence_names_what_was_missing():
    """告警要带上判断依据，否则用户和开发者都只能猜。"""
    warnings = _run()
    assert "工具调用 0 次" in warnings[0]["evidence"]


def test_real_tool_call_suppresses_warning():
    """真调了工具，哪怕任务后来失败，也不该由这里报警——失败原因在任务卡片里。"""
    assert _run(tool_names=("generate_image",)) == []


def test_media_task_ids_also_suppress_warning():
    """有的链路只留下 task_id 没有工具名（比如历史回放），同样算做过。"""
    assert _run(media_task_ids=("81fcd01d-3ed6-4946-88d3-aecdf90991cb",)) == []


def test_clarifying_question_is_not_a_warning():
    """模型反问「你想画什么风格」是正常的，不能误报。

    误报的代价比漏报高：用户会开始怀疑所有提示，包括真的那条。
    """
    assert _run(answer="你想画几张？什么风格？我先确认一下再提交。") == []


def test_intent_without_claim_is_not_a_warning():
    """用户要图，模型老实说「做不了」，这是诚实，不是撒谎。"""
    assert _run(answer="这个我做不了，生图后端现在不可用。") == []


def test_claim_without_intent_is_not_a_warning():
    """模型聊到「已提交」但用户这轮没要图，跟生图无关，不该报。"""
    assert _run(user_text="今天天气怎么样", answer="天气数据已经提交查询了。") == []


@pytest.mark.parametrize(
    "user_text",
    [
        "帮我生成每日的旅行路线以及可能看到的风景总共三张",
        "给我出几张图",
        "配图来一张",
        "画一张海边日落",
    ],
)
def test_various_image_phrasings_are_recognized(user_text):
    assert len(_run(user_text=user_text)) == 1


@pytest.mark.parametrize(
    "answer",
    [
        "三张图都提交生成好了。",
        "已经生成，稍等。",
        "图片正在生成中。",
        "已创建三个任务，出图后会显示。",
    ],
)
def test_various_claim_phrasings_are_recognized(answer):
    assert len(_run(answer=answer)) == 1
