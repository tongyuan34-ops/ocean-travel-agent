"""每轮补进上下文的事实块：把「它看不到、但真实发生了的事」说清楚。

**为什么需要这个模块（真实故障，2026-11-02）**

一个会话里模型连着三轮说「生图服务确实卡住了」，最后拒绝再生成。
翻 Redis 发现那三个任务全部 ``succeeded``——图早就生成完了，用户只是没在
页面上看到。模型不是不听指令，是它**没有任何途径知道任务成功了**：
``chat.py`` 组装历史时只喂 ``(role, content)``，``media_task_ids`` 和
``warnings`` 存了却从不进上下文。于是它只能从「用户说没看到图」反推
「任务失败」，再把这条错的因果当成后续每一轮推理的前提。

更糟的是它自己前几轮的假话（「三张图都提交好了」，实际 0 次工具调用）
也躺在历史里，被当成事实读回去——**谎话成了自己的证据**。

**分工**

- 本模块只**把事实写成文字**：纯函数，不查库、不判断该不该修、不碰事件流；
- 数据由调用方查好传进来（``MediaFact`` / ``WarningFact`` 两个值对象）；
- 好处是它能被纯函数测试钉住，不需要 mock Redis 或起服务。

**为什么并进 system prompt 而不是追加一条 system 消息**

部分服务商对多条 system 消息处理不一致，合并最稳——
和 ``app/core/context.py`` 里摘要的处理是同一个取舍。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# 最多回灌几个任务。够覆盖「上一轮提交的那几张」，又不至于让 token 随会话线性涨
MAX_FACTS_TASKS = 6

# 只截错误原因的前若干字：完整报错在任务卡片上，模型只需要知道「失败了、为什么」
_ERROR_CHARS = 90

_STATUS_TEXT = {
    "succeeded": "已成功生成，图已经在了",
    "failed": "失败了",
    "running": "还在生成中",
    "pending": "排队中，还没开始",
}


@dataclass(frozen=True)
class MediaFact:
    """一个生图任务的真实终态。字段都是快照，写的时候改不了。"""

    task_id: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class WarningFact:
    """上一轮的自检结论。告诉模型「你上一轮那句话不是事实」。"""

    code: str
    title: str


def recent_task_ids(messages: Iterable[object], limit: int = MAX_FACTS_TASKS) -> list[str]:
    """从后往前收集本会话最近出现过的 task_id，去重。

    取的是 ``session.messages`` 里的 ``media_task_ids``，不解析正文——
    正文里可能有用户贴的、或模型编的 id，那不是事实来源。
    """
    seen: list[str] = []
    for item in reversed(list(messages)):
        # 同一条消息里的 id 也是按时间先后存的，倒着读才是「最近优先」
        ids = getattr(item, "media_task_ids", None) or []
        for task_id in reversed(ids):
            if task_id and task_id not in seen:
                seen.append(task_id)
        if len(seen) >= limit:
            break
    return seen[:limit]


def last_warnings(messages: Iterable[object]) -> list[WarningFact]:
    """上一条**助手**消息的自检告警。

    只看助手消息：用户消息上没有告警，跳过它们才找得到真正要说的那一条。
    """
    for item in reversed(list(messages)):
        if getattr(item, "role", "") != "assistant":
            continue
        raw = getattr(item, "warnings", None) or []
        return [
            WarningFact(code=str(w.get("code", "")), title=str(w.get("title", "")))
            for w in raw
            if w.get("code")
        ]
    return []


def build_turn_facts(
    media: Sequence[MediaFact],
    warnings: Sequence[WarningFact],
) -> str:
    """拼出事实块。没有任何事实时返回空串——不占一个 token。"""
    lines: list[str] = []

    if media:
        lines.append("最近提交的生图任务（系统查证的真实状态）：")
        for fact in media:
            text = _STATUS_TEXT.get(fact.status, fact.status)
            line = f"- {fact.task_id[:8]}：{text}"
            if fact.status == "failed" and fact.error:
                line += f"（原因：{_brief(fact.error)}）"
            lines.append(line)

    if warnings:
        lines.append("上一轮的自检结果（系统比对工具调用记录后得出）：")
        for warn in warnings:
            lines.append(
                f"- {warn.code}：{warn.title}。"
                "这条表示你上一轮的说法与工具调用记录不一致，那句话不是事实，"
                "不要拿它当作推断服务状态的依据。"
            )

    if not lines:
        return ""

    head = "【系统提供的当前事实】以下由系统在发送前查证，优先于你之前说过的任何话："
    tail = (
        "用户说「没看到图」「图在哪」时，先对照上面的状态再回答："
        "状态是 succeeded 就说明图已经生成，问题在展示环节，"
        "应当让用户刷新页面或重开图片卡片，不要宣布服务故障，也不要因此拒绝再生成。"
    )
    return "\n".join([head, *lines, tail])


def _brief(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _ERROR_CHARS else flat[:_ERROR_CHARS] + "…"
