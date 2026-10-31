"""Token 用量与成本计量。

这里要分清两件事，混起来会同时错两次：

1. **预算控制**用 ``estimate_tokens`` 的启发式估算就够（宁可估多）；
2. **账单**必须用模型返回的真实 ``usage_metadata``，估算值不能当钱算。

早期版本只有 (1)，于是「这次对话花了多少钱」永远答不出来；
而如果直接拿 (1) 当账单，中英文混排时误差可以到 40% 以上。

因此 ``TokenUsage.estimated`` 标记这一轮的用量是否来自估算，
上层（SSE 的 end 事件、报表）必须把这个标记透出去，不能假装是精确值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.context import estimate_tokens

# 价目表：人民币元 / 百万 token，格式为 (输入单价, 输出单价)。
# 只用于本地成本估算，真实计费以服务商账单为准。
# 匹配方式是按前缀、长键优先，避免 "gpt-4o" 抢走 "gpt-4o-mini"。
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (1.10, 4.40),
    "gpt-4o": (18.00, 72.00),
    "qwen-plus": (0.80, 2.00),
    "qwen-turbo": (0.30, 0.60),
    "qwen-max": (2.40, 9.60),
    "deepseek-chat": (0.50, 2.00),
    "deepseek-reasoner": (1.00, 4.00),
    # DeepSeek 官方按美元计价（每百万 token），这里按 7.1 折算成人民币，取**峰时**
    # 且**未命中缓存**的那一档：成本报表低估会让人以为便宜，高估只是保守。
    # 峰时 01:00-04:00 / 06:00-10:00 UTC 的工作日，其余时段价格减半。
    "deepseek-flash": (2.13, 8.52),
    "deepseek-v4-pro": (9.37, 28.12),
    "glm-4-flash": (0.00, 0.00),
    "glm-4": (1.00, 1.00),
    "moonshot-v1-8k": (12.00, 12.00),
}

# 长键优先：否则 "gpt-4o" 会先命中 "gpt-4o-mini"
_KEYS_BY_LENGTH = sorted(PRICE_TABLE, key=len, reverse=True)


def resolve_price(
    model: str, override: tuple[float, float] | None = None
) -> tuple[float, float] | None:
    """解析模型的 (输入, 输出) 单价；未知模型返回 None，而不是 0。

    返回 None 与返回 (0, 0) 的含义完全不同：前者是「不知道」，
    后者是「确认免费」。混为一谈会让成本报表出现凭空的零。
    """
    if override is not None:
        return override
    name = (model or "").strip().lower()
    if not name:
        return None
    for key in _KEYS_BY_LENGTH:
        if name.startswith(key):
            return PRICE_TABLE[key]
    return None


@dataclass
class TokenUsage:
    """一段对话（单轮或整个会话）的 token 与成本累计。"""

    input_tokens: int = 0
    output_tokens: int = 0
    # 模型调用次数：native 模式下多轮工具调用会产生多次
    calls: int = 0
    # 是否包含估算值。只要有一次拿不到真实 usage，整段就标记为估算
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int, *, estimated: bool = False) -> None:
        self.input_tokens += max(0, int(input_tokens))
        self.output_tokens += max(0, int(output_tokens))
        self.calls += 1
        self.estimated = self.estimated or estimated

    def merge(self, other: TokenUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls
        self.estimated = self.estimated or other.estimated

    def to_dict(
        self,
        model: str = "",
        price: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        """序列化。成本为 None 表示价目表里没有这个模型，不是零。"""
        resolved = resolve_price(model, price)
        cost = (
            None
            if resolved is None
            else round(
                self.input_tokens / 1_000_000 * resolved[0]
                + self.output_tokens / 1_000_000 * resolved[1],
                6,
            )
        )
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "cost": cost,
            "currency": "CNY",
            "estimated": self.estimated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TokenUsage:
        """从持久化字典恢复，容忍字段缺失（老会话没有 usage 字段）。"""
        if not data:
            return cls()
        return cls(
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            calls=int(data.get("calls") or 0),
            estimated=bool(data.get("estimated") or False),
        )


def usage_from_message(message: Any) -> tuple[int, int] | None:
    """从一条 AIMessage 里取真实用量，取不到返回 None。

    两个来源都要看：langchain 标准字段是 ``usage_metadata``，
    但部分 OpenAI 兼容服务只填 ``response_metadata.token_usage``
    （OpenAI 原始命名 prompt_tokens / completion_tokens）。
    """
    meta = getattr(message, "usage_metadata", None)
    if isinstance(meta, dict):
        input_tokens = int(meta.get("input_tokens") or 0)
        output_tokens = int(meta.get("output_tokens") or 0)
        if input_tokens or output_tokens:
            return input_tokens, output_tokens

    raw = getattr(message, "response_metadata", None)
    if isinstance(raw, dict):
        token_usage = raw.get("token_usage") or raw.get("usage")
        if isinstance(token_usage, dict):
            input_tokens = int(
                token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
            )
            output_tokens = int(
                token_usage.get("completion_tokens") or token_usage.get("output_tokens") or 0
            )
            if input_tokens or output_tokens:
                return input_tokens, output_tokens

    return None


def estimate_usage(prompt_texts: list[str], answer: str) -> tuple[int, int]:
    """拿不到真实用量时的兜底估算。

    只用于「至少有个数」的场景，调用方必须把 ``estimated`` 标成 True。
    """
    return sum(estimate_tokens(text) for text in prompt_texts), estimate_tokens(answer)


def usage_from_output(output: Any) -> tuple[int, int] | None:
    """从 astream_events 的 ``on_chat_model_end`` 输出里取用量。

    这个字段的形状随版本和模式变化：可能是 AIMessage、ChatResult、
    或 LLMResult（``generations[0].message``）。三种都试一遍，
    比在某处硬编码一种形状要稳。
    """
    direct = usage_from_message(output)
    if direct is not None:
        return direct

    generations = getattr(output, "generations", None)
    if generations:
        first = generations[0]
        found = usage_from_message(getattr(first, "message", first))
        if found is not None:
            return found

    message = getattr(output, "message", None)
    if message is not None:
        return usage_from_message(message)

    return None


__all__ = [
    "PRICE_TABLE",
    "TokenUsage",
    "estimate_usage",
    "resolve_price",
    "usage_from_message",
    "usage_from_output",
]
