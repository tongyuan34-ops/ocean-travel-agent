"""SQLite 会话存储：多租户部署下的持久化后端。

**为什么不再用一个 JSON 文件**

``FileSessionStore`` 的两处代价随规模线性放大：

- **写入**：每次 ``save`` 都要把**所有**会话重新拼成一份 JSON 再整份替换。
  会话越多，追加一条消息越慢，而且是平方级的字符串拼接。
- **读取**：启动时全量解析进内存；``list(tenant)`` 只能在内存里过滤，
  租户一多，每个租户的列表请求都要扫一遍所有人的会话。

SQLite 把这两件事都变成「一行」：写入是 ``INSERT OR REPLACE`` 一行，
列表走 ``tenant`` 索引。仍然只有一个文件，不需要额外服务——桌面版和
服务器都能用，迁移就是换个路径。

**表结构为什么把整条会话塞进 data 列**

会话是一个整体（消息、摘要、用量、风格一起读一起写），拆成 messages 表
除了多几次 join 之外没有收益，反而让「摘要折叠」这种跨字段操作变复杂。
真正需要按列查的只有 ``tenant``，所以只把它提成列并建索引。

**并发与事务**

- 一个连接 + ``threading.Lock``：sqlite3 的连接不是线程安全的，而
  ``asyncio.to_thread`` 会让多个数据库操作真正并行，必须在连接外面串行化。
- WAL + ``synchronous=NORMAL``：崩溃最多丢最后一个事务，换来每次
  ``save`` 不 fsync。这里存的是聊天记录，不是账本——丢最后一轮可以接受，
  每追加一条消息都 fsync 会让 SSE 明显变慢。
- ``busy_timeout``：允许多进程同时开同一个文件（桌面版多开），
  写冲突时等一会儿而不是立刻 ``database is locked``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.storage.base import SessionStore
from app.storage.models import ChatSession

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_T = TypeVar("_T")


def _parse(raw: str, session_id: str) -> ChatSession | None:
    """单条记录解析失败只跳过它，不能让整个会话库变成空的。"""
    try:
        return ChatSession.from_dict(json.loads(raw))
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("跳过一条无法解析的会话记录 | id=%s：%s", session_id, exc)
        return None


class SQLiteSessionStore(SessionStore):
    """会话持久化到单个 SQLite 文件，按租户建索引。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn = self._connect(path)
        self._init_schema()
        count = self._count()
        logger.info("会话存储：SQLite（%s，%d 条）", path, count)

    # ---------- 连接与建表 ----------

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        # 父目录不存在就建；建不出来直接抛——启动时报错比每条消息静默丢失好查
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "id TEXT PRIMARY KEY,"
                "tenant TEXT NOT NULL DEFAULT 'default',"
                "data TEXT NOT NULL)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant)")
            # 预留演进位：以后加列/改结构先看这个版本号，而不是靠猜
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _count(self) -> int:
        try:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        except sqlite3.Error as exc:  # pragma: no cover - 打不开就不该走到这里
            logger.warning("会话计数失败：%s", exc)
            return 0
        return int(row[0]) if row else 0

    # ---------- 线程化 ----------

    async def _run(self, fn: Callable[..., _T], *args: object) -> _T:
        """把同步的 sqlite 操作丢到线程里，事件循环不被磁盘 IO 卡住。"""

        def locked() -> _T:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    # ---------- 同步实现（都在线程里跑） ----------

    def _upsert(self, session: ChatSession) -> None:
        payload = json.dumps(session.to_dict(), ensure_ascii=False)
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO sessions (id, tenant, data) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "tenant = excluded.tenant, data = excluded.data",
                    (session.id, session.tenant, payload),
                )
        except sqlite3.Error as exc:
            # 和文件后端一致：落盘失败只记日志，不让这一轮对话崩掉
            logger.error("会话写入失败（本次改动未落库）：%s", exc)

    def _get(self, session_id: str) -> ChatSession | None:
        try:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error("会话读取失败：%s", exc)
            return None
        return _parse(row[0], session_id) if row else None

    def _delete(self, session_id: str) -> None:
        try:
            with self._conn:
                self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        except sqlite3.Error as exc:
            logger.error("会话删除失败：%s", exc)

    def _list(self, tenant: str | None) -> list[ChatSession]:
        sql: str
        params: tuple[str, ...]
        if tenant is None:
            sql, params = "SELECT id, data FROM sessions ORDER BY rowid", ()
        else:
            # 走 idx_sessions_tenant，不用把所有租户的会话都解析一遍
            sql, params = (
                "SELECT id, data FROM sessions WHERE tenant = ? ORDER BY rowid",
                (tenant,),
            )
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.error("会话列表读取失败：%s", exc)
            return []
        sessions = []
        for session_id, raw in rows:
            session = _parse(raw, session_id)
            if session is not None:
                sessions.append(session)
        return sessions

    def _ping(self) -> bool:
        try:
            return self._conn.execute("SELECT 1").fetchone() is not None
        except sqlite3.Error:
            return False

    def _close(self) -> None:
        # 退出路径可能连关两次（lifespan + 显式 aclose），重复关闭不该抛
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def bulk_upsert(self, sessions: list[ChatSession]) -> int:
        """一次事务写入多条（启动时导入旧数据用）。同步执行，不经过事件循环。"""
        if not sessions:
            return 0
        rows = [
            (session.id, session.tenant, json.dumps(session.to_dict(), ensure_ascii=False))
            for session in sessions
        ]
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT INTO sessions (id, tenant, data) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "tenant = excluded.tenant, data = excluded.data",
                    rows,
                )
        except sqlite3.Error as exc:
            logger.error("批量导入失败：%s", exc)
            return 0
        return len(rows)

    def close(self) -> None:
        """同步关闭，供启动阶段的一次性导入回收连接。"""
        with self._lock:
            self._close()

    # ---------- 接口实现 ----------

    async def create(self, session: ChatSession) -> None:
        await self._run(self._upsert, session)

    async def get(self, session_id: str) -> ChatSession | None:
        return await self._run(self._get, session_id)

    async def save(self, session: ChatSession) -> None:
        # 新建与更新共用一条 upsert：上层不需要区分，重复调用也安全
        await self._run(self._upsert, session)

    async def delete(self, session_id: str) -> None:
        await self._run(self._delete, session_id)

    async def list(self, tenant: str | None = None) -> list[ChatSession]:
        return await self._run(self._list, tenant)

    async def ping(self) -> bool:
        return await self._run(self._ping)

    async def aclose(self) -> None:
        await self._run(self._close)
