"""token 用量与成本计量。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from app.core.usage import (
    TokenUsage,
    estimate_usage,
    resolve_price,
    usage_from_message,
    usage_from_output,
)


def test_price_prefers_longer_model_name():
    """gpt-4o-mini 不能被 gpt-4o 抢走——前缀匹配顺序错了会把成本算高十倍。"""
    assert resolve_price("gpt-4o-mini") == (1.10, 4.40)
    assert resolve_price("gpt-4o") == (18.00, 72.00)
    # 带日期后缀的版本号也要能匹配
    assert resolve_price("gpt-4o-mini-2024-07-18") == (1.10, 4.40)


def test_unknown_model_price_is_none_not_zero():
    """未知模型返回 None；返回 0 会让成本报表出现凭空的「免费」。"""
    assert resolve_price("some-local-gguf-model") is None
    assert resolve_price("") is None


def test_price_override_wins():
    assert resolve_price("gpt-4o", (1.0, 2.0)) == (1.0, 2.0)


def test_usage_accumulates_and_marks_estimated():
    usage = TokenUsage()
    usage.add(100, 20)
    usage.add(50, 10, estimated=True)

    assert usage.input_tokens == 150
    assert usage.output_tokens == 30
    assert usage.total_tokens == 180
    assert usage.calls == 2
    # 只要有一次是估算，整段就不能声称精确
    assert usage.estimated is True


def test_usage_merge_keeps_estimated_flag():
    session = TokenUsage()
    session.add(100, 10)
    turn = TokenUsage()
    turn.add(10, 1, estimated=True)
    session.merge(turn)

    assert session.input_tokens == 110
    assert session.calls == 2
    assert session.estimated is True


def test_to_dict_computes_cost():
    usage = TokenUsage()
    usage.add(1_000_000, 0)
    data = usage.to_dict("qwen-plus")

    assert data["cost"] == 0.8  # 输入 0.8 元 / 百万 token
    assert data["currency"] == "CNY"
    assert data["estimated"] is False


def test_to_dict_cost_none_when_model_unknown():
    usage = TokenUsage()
    usage.add(1000, 100)
    assert usage.to_dict("mystery-model")["cost"] is None


def test_from_dict_tolerates_missing_fields():
    """老会话没有 usage 字段，恢复时不能炸。"""
    assert TokenUsage.from_dict(None).total_tokens == 0
    assert TokenUsage.from_dict({}).calls == 0
    restored = TokenUsage.from_dict({"input_tokens": 7, "output_tokens": 3, "turns": 2})
    assert restored.total_tokens == 10
    assert restored.calls == 0


def test_usage_from_message_reads_usage_metadata():
    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
    )
    assert usage_from_message(message) == (12, 5)


def test_usage_from_message_falls_back_to_response_metadata():
    """部分 OpenAI 兼容服务只填 response_metadata.token_usage。"""
    message = AIMessage(
        content="hi",
        response_metadata={"token_usage": {"prompt_tokens": 9, "completion_tokens": 4}},
    )
    assert usage_from_message(message) == (9, 4)


def test_usage_from_message_returns_none_without_usage():
    assert usage_from_message(AIMessage(content="hi")) is None


def test_usage_from_output_handles_wrapped_shapes():
    """on_chat_model_end 的输出可能是消息、ChatResult 或 LLMResult。"""

    class _Generation:
        def __init__(self, message: AIMessage) -> None:
            self.message = message

    class _Result:
        def __init__(self, message: AIMessage) -> None:
            self.generations = [_Generation(message)]

    message = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    )
    assert usage_from_output(message) == (3, 2)
    assert usage_from_output(_Result(message)) == (3, 2)
    assert usage_from_output(object()) is None


def test_estimate_usage_counts_cjk():
    prompt_tokens, answer_tokens = estimate_usage(["杭州明天天气"], "多云")
    assert prompt_tokens > 0
    assert answer_tokens > 0
