"""上下文预算与滚动摘要。

落实 SPEC 2.4 与 4.5(e)：
- MAX_CONTEXT_TOKENS 默认 4096，保留 system prompt 与最近若干轮原文
- 超出预算时把更早的对话滚动摘要，压缩进 system prompt
- 工具返回的大段内容先截断再进入上下文，避免单次调用塞爆窗口
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

# 始终保留原文的最近轮数（1 轮 = 一问一答）
KEEP_RECENT_TURNS = 6
# 每次摘要至少折叠多少条消息，避免每轮都触发一次摘要调用（摊销成本）
SUMMARY_BATCH = 6
# 单条工具结果进入上下文前的字符上限
TOOL_OUTPUT_LIMIT = 1200
# 摘要长度上限
SUMMARY_MAX_CHARS = 300

_SUMMARY_INSTRUCTION = (
    "你是旅行助手的记忆压缩器。把对话压缩成一段简洁摘要，必须保留："
    "目的地与日期、人数、预算、已确认的偏好与结论、用户明确否决的选项、尚未解决的问题。"
    "只依据给定内容，严禁编造。不要写寒暄与过程描述。"
    f"只输出摘要正文，不超过 {SUMMARY_MAX_CHARS} 字。"
)


# ---------- token 估算 ----------


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # 汉字
        or 0x3000 <= code <= 0x303F  # 中文标点
        or 0x3040 <= code <= 0x30FF  # 日文假名
        or 0xFF00 <= code <= 0xFFEF  # 全角符号
    )


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    中日韩字符按 1 token，其余按 4 字符 1 token，向上取整。
    宁可略高估，避免实际超窗口。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    rest = len(text) - cjk
    return cjk + (rest + 3) // 4


def estimate_messages_tokens(messages: list[BaseMessage]) -> int:
    return sum(estimate_tokens(_as_text(message.content)) for message in messages)


# ---------- 工具输出截断 ----------


def truncate_tool_output(text: str, limit: int = TOOL_OUTPUT_LIMIT) -> str:
    """工具返回的大段内容在进入上下文前先截断。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（内容过长已截断，原文共 {len(text)} 字）"


# ---------- 折叠规划 ----------


@dataclass
class FoldPlan:
    """本轮需要折叠进摘要的历史，以及折叠后的新计数。"""

    fold: list[tuple[str, str]] = field(default_factory=list)
    new_summarized_count: int = 0

    @property
    def needed(self) -> bool:
        return bool(self.fold)


def plan_fold(
    *,
    system_prompt: str,
    summary: str,
    history: list[tuple[str, str]],
    summarized_count: int,
    max_tokens: int,
) -> FoldPlan:
    """决定把哪些历史消息折叠进摘要。

    history 为 [(role, content)]，role 取 user 或 assistant。
    """
    recent_keep = KEEP_RECENT_TURNS * 2
    total = len(history)
    index = summarized_count
    fold: list[tuple[str, str]] = []

    # 规则一：攒够一批再折叠，避免每轮都多一次摘要调用
    if total - index > recent_keep + SUMMARY_BATCH:
        fold_count = total - index - recent_keep
        fold = list(history[index : index + fold_count])
        index += fold_count

    # 规则二：仍超预算时继续从最旧一端折叠（至少留最后一轮）
    while (
        estimate_messages_tokens(compose_messages(system_prompt, summary, history, index))
        > max_tokens
        and total - index > 2
    ):
        fold.append(history[index])
        index += 1

    if fold:
        logger.info(
            "触发上下文摘要：折叠 %d 条历史，保留最近 %d 条", len(fold), total - index
        )

    return FoldPlan(fold=fold, new_summarized_count=index)


def compose_messages(
    system_prompt: str,
    summary: str,
    history: list[tuple[str, str]],
    summarized_count: int,
) -> list[BaseMessage]:
    """拼出送给模型的消息列表。

    摘要并入 system prompt，而不是再追加一条 system 消息——
    部分服务商对多条 system 消息处理不一致，合并最稳。
    """
    system_text = system_prompt
    if summary:
        system_text = f"{system_prompt}\n\n【此前对话摘要】\n{summary}"

    messages: list[BaseMessage] = [SystemMessage(content=system_text)]
    for role, content in history[summarized_count:]:
        if role == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


# ---------- 摘要生成 ----------


async def make_summary(
    model: BaseChatModel,
    previous_summary: str,
    fold: list[tuple[str, str]],
) -> str:
    """把历史折叠进摘要，返回新的摘要文本。失败时抛异常，由调用方决定是否回退。"""
    transcript = "\n".join(
        f"{'用户' if role == 'user' else '助手'}：{content}" for role, content in fold
    )
    parts = [_SUMMARY_INSTRUCTION]
    if previous_summary:
        parts.append(f"【已有摘要】\n{previous_summary}")
    parts.append(f"【新增对话】\n{transcript}")

    reply = await model.ainvoke("\n\n".join(parts))
    text = _as_text(getattr(reply, "content", reply)).strip()
    if not text:
        raise ValueError("模型未返回摘要内容")
    return text[:SUMMARY_MAX_CHARS]


# ---------- 辅助 ----------


def _as_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    return str(content or "")
