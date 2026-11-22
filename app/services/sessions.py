"""会话目录：创建/读取/删除、列表投影、风格与技能切换。

单独成服务的理由：这些操作全在「会话」这一层，与一轮对话怎么跑无关。
放在 ChatService 里时，路由要拿一个会发起模型调用的对象来列个会话列表，
既难讲清边界，也让测试必须构造整个聊天服务才能验租户隔离。
持久化仍由注入的 SessionStore 负责，这里不关心后端是 JSON 还是 Redis。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import uuid4

from app.agent.prompts import get_persona
from app.core.errors import ApiError
from app.core.identity import DEFAULT_TENANT
from app.skills import get_skill
from app.storage import ChatSession, SessionStore


class SessionRow(TypedDict):
    """会话列表里的一行。

    用 TypedDict 而不是 dict[str, Any]：排序要按 updated_at，
    键写成 Any 的话 mypy 认不出它是可比较的字符串。
    """

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


# 会话列表里的标题长度。和前端发送后立刻显示的那版保持一致（20 字 + 省略号），
# 否则重开程序后同一个会话的标题会突然变长，看着像出了 bug
TITLE_LIMIT = 20


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def session_title(session: ChatSession) -> str:
    """会话标题：取首条用户消息的第一行。

    不单独存一个 title 字段：用户改不了它，存下来只会和首条消息漂移
    （重试、编辑首条消息之后就对不上了）。
    """
    for message in session.messages:
        if message.role != "user":
            continue
        text = " ".join((message.content or "").split())
        if text:
            return text if len(text) <= TITLE_LIMIT else f"{text[:TITLE_LIMIT]}…"
    return "新的旅程"


def session_updated_at(session: ChatSession) -> str:
    """最后一条消息的时间；没有消息就退回创建时间。"""
    for message in reversed(session.messages):
        if message.created_at:
            return message.created_at
    return session.created_at


class SessionService:
    """会话的增删查与设置项。所有读取都按租户过滤。"""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def create(self, tenant: str = DEFAULT_TENANT) -> ChatSession:
        # 租户在创建时盖章，之后这个会话就只属于它。读取方一律带着自己的租户来比对，
        # 而不是在列表/详情里各写一遍过滤——少一处漏写就是一个越权入口
        session = ChatSession(id=str(uuid4()), created_at=now_iso(), tenant=tenant)
        await self._store.create(session)
        return session

    async def get(self, session_id: str, tenant: str | None = None) -> ChatSession:
        session = await self._store.get(session_id)
        # 不属于这个租户的会话按「不存在」处理，而不是 403：
        # 403 等于告诉对方「这个 id 是存在的」，会话 id 就成了可枚举的资源
        if session is None or (tenant is not None and session.tenant != tenant):
            raise ApiError("SESSION_NOT_FOUND", f"会话不存在：{session_id}")
        return session

    async def save(self, session: ChatSession) -> None:
        await self._store.save(session)

    async def delete(self, session_id: str, tenant: str | None = None) -> None:
        if tenant is not None:
            session = await self._store.get(session_id)
            # 不存在的会话仍然按幂等删除处理（重复点删除不该报错），
            # 但别人的会话必须挡住
            if session is not None and session.tenant != tenant:
                raise ApiError("SESSION_NOT_FOUND", f"会话不存在：{session_id}")
        await self._store.delete(session_id)

    async def list_rows(self, tenant: str | None = None) -> list[SessionRow]:
        """列出全部会话，最近的排前面。

        **为什么要后端出这份列表**：桌面版每次启动端口随机，而 localStorage
        按「源（含端口）」隔离——重开程序等于换了一个新源，前端记的会话列表
        是空的。用户看到「历史消息全没了」，其实数据一直在 ``data/sessions.json``
        里，只是没人问后端要。列表改由后端给，跟端口、跟浏览器缓存都无关。
        """
        sessions = await self._store.list(tenant)
        items: list[SessionRow] = [
            {
                "id": session.id,
                "title": session_title(session),
                "created_at": session.created_at,
                "updated_at": session_updated_at(session),
                "message_count": len(session.messages),
            }
            for session in sessions
        ]
        # 按最后一条消息的时间倒序；空会话退回创建时间，不至于沉到列表底部
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items

    async def usage(self, session_id: str, tenant: str | None = None) -> dict[str, Any]:
        """会话累计用量。前端用来展示「本会话花了多少」。"""
        session = await self.get(session_id, tenant)
        return dict(session.usage)

    async def set_persona(self, session_id: str, persona: str, tenant: str | None = None) -> str:
        """切换对话风格，返回实际生效的 key。

        未知 key 不报错而是回落默认（见 ``get_persona``）：前端选项来自
        ``GET /personas``，正常不会传错；真传错了，让整轮对话 500 也没好处。

        切换只影响**后续轮次**的 system prompt，不改历史消息——
        已经聊过的内容按当时的风格生成，这是事实，不该被追溯修改。
        """
        session = await self.get(session_id, tenant)
        resolved = get_persona(persona)
        session.persona = resolved.key
        await self._store.save(session)
        return resolved.key

    async def set_skill(self, session_id: str, skill: str, tenant: str | None = None) -> str:
        """切换本会话固定使用的技能；空串表示回到「不指定」。

        与 persona 不同，未知 key 不回落而是报错：技能会带来额外的工具与提示词，
        「选了一个不存在的技能却静默用默认」会让用户以为技能在生效。
        清空时连同 ``active_skills`` 一起清掉——否则下一轮又会按「追问沿用」
        把刚取消的技能重新激活。
        """
        session = await self.get(session_id, tenant)
        key = (skill or "").strip().lower()
        if key:
            item = get_skill(key)
            if item is None:
                raise ApiError("INVALID_REQUEST", f"没有这个技能：{skill}")
            key = item.key
        else:
            session.active_skills = []
        session.skill = key
        await self._store.save(session)
        return key
