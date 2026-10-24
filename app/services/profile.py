"""用户长期偏好：抽取、合并、注入系统提示词。

设计要点：
- 偏好按设备隔离（user_id 来自 X-Device-ID），存 Redis，重启不丢；
- 抽取在回答完成后**后台**跑，不阻塞 SSE；
- 抽取结果先落「候选」，等前端确认后才写入正式偏好——写记忆是有副作用的操作；
- 注入提示词时只带最多 12 条、600 字，避免挤占上下文预算。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.agent.model import build_chat_model
from app.core.config import Settings
from app.storage.profile import ProfileStore

logger = logging.getLogger(__name__)

MAX_ITEMS = 20
MAX_SUGGESTIONS = 3
MAX_PROMPT_ITEMS = 12
MAX_PROMPT_CHARS = 600
_HISTORY_MESSAGES = 6
_HISTORY_CHARS = 500
_EXTRACT_TIMEOUT_SEC = 90

# 只有出现这些信号才触发抽取，避免每轮都多一次模型调用
_SIGNAL_WORDS = (
    "记住",
    "偏好",
    "喜欢",
    "讨厌",
    "不爱",
    "不吃",
    "忌口",
    "过敏",
    "习惯",
    "每次",
    "总是",
    "从不",
    "省钱",
    "不想",
    "不能",
    "带娃",
    "小孩",
    "老人",
    "自驾",
    "早起",
    "怕",
)

_EXTRA_INSTRUCTION = """你是旅行助手的信息提取器。从下面的对话里提取「用户长期稳定的旅行偏好」。

只提取对以后规划有用、且用户**明确表达**过的内容，归入这些类别之一：
节奏 / 预算 / 住宿 / 交通 / 饮食 / 同行人 / 其他

不要提取：
- 本次行程的具体日期、地名、一次性安排
- 身份证、手机号、支付信息、住址等敏感信息
- 助手推测或用户没有明确说过的内容

输出严格 JSON，最多 3 条：
{"items":[{"category":"饮食","text":"不吃辣"}]}
没有可提取内容时输出：{"items":[]}
只输出 JSON，不要解释。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _norm(text: str) -> str:
    """去空白、转小写，用于判重。"""
    return re.sub(r"\s+", "", str(text)).lower()


def _parse_items(raw: Any) -> list[dict[str, str]]:
    """把模型输出解析成 [{category, text}]；解析失败返回空列表。"""
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = str(item.get("text") or "").strip()
        if not value:
            continue
        cleaned.append(
            {
                "category": str(item.get("category") or "其他").strip()[:20] or "其他",
                "text": value[:120],
            }
        )
        if len(cleaned) >= MAX_SUGGESTIONS:
            break
    return cleaned


class ProfileService:
    """偏好读写、候选抽取与提示词渲染。"""

    def __init__(self, settings: Settings, store: ProfileStore) -> None:
        self._settings = settings
        self._store = store
        self._model: Any = None

    # ---------- 正式偏好 ----------

    async def get(self, user_id: str) -> dict[str, Any]:
        data = await self._store.get_profile(user_id)
        if not data:
            return {"items": [], "updated_at": ""}
        items = data.get("items")
        return {
            "items": items if isinstance(items, list) else [],
            "updated_at": str(data.get("updated_at") or ""),
        }

    async def add(self, user_id: str, candidates: list[dict[str, str]]) -> dict[str, Any]:
        """合并候选到正式偏好：按文本判重，最多保留 MAX_ITEMS 条。"""
        profile = await self.get(user_id)
        items: list[dict[str, Any]] = list(profile["items"])
        existing = {_norm(item.get("text", "")) for item in items}

        for candidate in candidates:
            text = str(candidate.get("text") or "").strip()[:120]
            if not text or _norm(text) in existing:
                continue
            items.append(
                {
                    "id": str(uuid4()),
                    "category": str(candidate.get("category") or "其他")[:20],
                    "text": text,
                    "created_at": _now(),
                }
            )
            existing.add(_norm(text))

        items = items[-MAX_ITEMS:]
        await self._store.save_profile(user_id, {"items": items, "updated_at": _now()})
        return {"items": items, "updated_at": _now()}

    async def remove(self, user_id: str, item_id: str) -> None:
        profile = await self.get(user_id)
        items = [item for item in profile["items"] if item.get("id") != item_id]
        await self._store.save_profile(user_id, {"items": items, "updated_at": _now()})

    async def clear(self, user_id: str) -> None:
        await self._store.delete_profile(user_id)

    # ---------- 候选 ----------

    async def get_suggestions(self, user_id: str) -> list[dict[str, str]]:
        data = await self._store.get_suggestions(user_id)
        items = (data or {}).get("items")
        return items if isinstance(items, list) else []

    async def dismiss_suggestions(self, user_id: str) -> None:
        await self._store.delete_suggestions(user_id)

    async def maybe_suggest(
        self, user_id: str, user_text: str, history: list[tuple[str, str]]
    ) -> None:
        """回答完成后调用：命中信号词才抽取，结果存为待确认候选。

        任何异常都在这里吞掉——抽偏好失败不能影响下一次聊天。
        """
        try:
            if not any(word in (user_text or "") for word in _SIGNAL_WORDS):
                return
            profile = await self.get(user_id)
            known = [str(item.get("text") or "") for item in profile["items"]]
            transcript = _transcript(history)
            if not transcript:
                return

            prompt = (
                f"{_EXTRA_INSTRUCTION}\n\n"
                f"【已有偏好（不要重复提取）】\n"
                + ("\n".join(f"- {text}" for text in known) or "（暂无）")
                + f"\n\n【本轮对话】\n{transcript}"
            )
            if self._model is None:
                self._model = build_chat_model(self._settings)
            reply = await asyncio.wait_for(
                self._model.ainvoke(prompt), timeout=_EXTRACT_TIMEOUT_SEC
            )
            parsed = _parse_items(getattr(reply, "content", reply))

            existing = {_norm(text) for text in known}
            items = [
                {"id": str(uuid4()), "category": item["category"], "text": item["text"]}
                for item in parsed
                if _norm(item["text"]) not in existing
            ]
            if not items:
                return
            await self._store.save_suggestions(
                user_id, {"items": items[:MAX_SUGGESTIONS], "created_at": _now()}
            )
            logger.info("已生成 %d 条偏好候选 | user=%s", len(items), user_id)
        except Exception as exc:  # noqa: BLE001 - 抽取失败只记日志
            logger.warning("偏好抽取失败（user=%s）：%s", user_id, exc)

    # ---------- 注入提示词 ----------

    async def render_block(self, user_id: str) -> str:
        """渲染成可拼在系统提示词后的文本；没有偏好时返回空串。"""
        profile = await self.get(user_id)
        items = profile["items"][:MAX_PROMPT_ITEMS]
        if not items:
            return ""
        lines = [
            f"- {item.get('category') or '其他'}：{item.get('text') or ''}"
            for item in items
            if item.get("text")
        ]
        body = "\n".join(lines)
        if len(body) > MAX_PROMPT_CHARS:
            body = body[:MAX_PROMPT_CHARS] + "…"
        return (
            "【用户长期偏好（规划时优先满足）】\n"
            f"{body}\n"
            "以上偏好来自用户本人；若与用户本次明确要求冲突，以本次要求为准。"
        )

    async def aclose(self) -> None:
        await self._store.aclose()


def _transcript(history: list[tuple[str, str]]) -> str:
    """取最近几条对话，逐条截断，避免抽取请求过长。"""
    recent = history[-_HISTORY_MESSAGES:]
    lines: list[str] = []
    for role, content in recent:
        text = str(content or "").strip()
        if not text:
            continue
        if len(text) > _HISTORY_CHARS:
            text = text[:_HISTORY_CHARS] + "…"
        lines.append(f"{'用户' if role == 'user' else '助手'}：{text}")
    return "\n".join(lines)


def build_profile_service(settings: Settings) -> ProfileService:
    """构造偏好服务；存储后端跟随 STORAGE_BACKEND。"""
    from app.storage.profile import build_profile_store

    return ProfileService(settings, build_profile_store(settings))
