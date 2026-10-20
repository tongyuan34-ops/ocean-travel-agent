"""上下文预算、工具输出截断与滚动摘要规划。

对应 SPEC 2.4（上下文预算与多轮记忆）与 4.5(e)。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from app.core.context import (
    TOOL_OUTPUT_LIMIT,
    compose_messages,
    estimate_tokens,
    plan_fold,
    truncate_tool_output,
)

SYSTEM = "你是旅行规划助手。"


def test_truncate_short_text_untouched():
    assert truncate_tool_output("短文本") == "短文本"


def test_truncate_long_text_marks_and_limits():
    text = "字" * (TOOL_OUTPUT_LIMIT + 500)
    out = truncate_tool_output(text)
    assert "已截断" in out
    assert out.startswith("字" * 10)
    # 截断后长度应接近上限，而不是原长
    assert len(out) < len(text)


def test_estimate_tokens_counts_cjk_and_latin_differently():
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcdefgh") == 2  # 4 字符 ≈ 1 token


def test_compose_merges_summary_into_system_prompt():
    """摘要并入 system prompt，而不是追加第二条 system 消息——
    部分服务商对多条 system 消息处理不一致。"""
    messages = compose_messages(SYSTEM, "用户预算 8000", [("user", "你好")], 0)
    assert len(messages) == 2
    assert messages[0].type == "system"
    assert "用户预算 8000" in messages[0].content
    assert messages[1].content == "你好"


def test_compose_drops_summarized_history():
    history = [("user", "旧消息"), ("assistant", "旧回答"), ("user", "新消息")]
    messages = compose_messages(SYSTEM, "摘要", history, 2)
    contents = [m.content for m in messages]
    assert "旧消息" not in contents
    assert "新消息" in contents


def test_plan_fold_keeps_recent_turns():
    """攒够一批才折叠，避免每轮都多一次摘要调用。"""
    history = [(("user" if i % 2 == 0 else "assistant"), f"第{i}条") for i in range(20)]
    plan = plan_fold(
        system_prompt=SYSTEM,
        summary="",
        history=history,
        summarized_count=0,
        max_tokens=100_000,
    )
    assert plan.needed
    # 最近 KEEP_RECENT_TURNS*2 = 12 条必须保留原文
    assert len(history) - plan.new_summarized_count >= 12


def test_plan_fold_noop_when_short():
    plan = plan_fold(
        system_prompt=SYSTEM,
        summary="",
        history=[("user", "你好"), ("assistant", "在")],
        summarized_count=0,
        max_tokens=4096,
    )
    assert not plan.needed


def test_plan_fold_respects_token_budget():
    """即使轮数没超，超出 token 预算也要继续折叠。"""
    history = [(("user" if i % 2 == 0 else "assistant"), "长" * 300) for i in range(10)]
    plan = plan_fold(
        system_prompt=SYSTEM,
        summary="",
        history=history,
        summarized_count=0,
        max_tokens=600,
    )
    assert plan.needed
    assert len(history) - plan.new_summarized_count <= 8


def test_as_text_handles_content_blocks():
    """_extract_answer 依赖把 list content 拼成文本（OpenAI 风格分块）。"""
    from app.services.chat import _as_text

    assert _as_text("纯文本") == "纯文本"
    assert _as_text([{"type": "text", "text": "片段"}]) == "片段"
    assert _as_text(None) == ""


def test_extract_answer_prefers_final_ai_message():
    from app.services.chat import _extract_answer

    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}]),
            AIMessage(content="最终回答"),
        ]
    }
    answer, reason = _extract_answer(state, "流式内容")
    assert answer == "最终回答"
    assert reason == "stop"
