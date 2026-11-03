"""文件会话存储：把会话落到一个 JSON 文件，供桌面版使用。

**为什么需要第三种后端**

- ``memory``：进程一退就没了。桌面版用户双击 exe、关掉窗口，聊天记录全丢——
  这不是「开发时才遇到」，是每个用户第一次关程序就会发现的问题。
- ``redis``：多进程部署的正确选择，但要求用户装一个 Redis。桌面软件不能这么要求。
- ``file``：单用户单进程，一个 JSON 文件就够。没有额外依赖，卸载时删掉即可。

**为什么继承 MemorySessionStore**

读写逻辑（深拷贝、加锁）完全一样，差别只在「要不要落盘」。
组合一个 ``MemorySessionStore`` 再包一层持久化，比复制一遍 CRUD 更不容易走样——
而且以后内存实现改行为，这里自动跟上。

**写入策略：原子 + 不阻塞事件循环 + 只重写变过的部分**

必须原子：先写 ``.tmp`` 再 ``os.replace``，否则断电或崩溃会留下一个半截的 JSON，
下次启动直接读不出来，用户看到的是「所有记录都没了」——比慢一点严重得多。

但「整份重写」在会话多起来之后有两处代价。实测 200 个会话 × 50 条消息：

- ``to_dict`` + ``json.dumps`` 占 26ms，写文件占 7ms，合计 33ms；
- 这 33ms 原本是**同步**跑在事件循环里的——每追加一条消息就阻塞一次，
  期间 SSE 的心跳和别的请求全停。

所以这里做两件事：

1. **序列化缓存**：每个会话上次序列化出来的 JSON 文本留着，下次只有变过的
   会话需要重新 ``to_dict`` + ``dumps``。改一个会话的成本从「全部」降到「一个」。
2. **写盘挪到线程**：``asyncio.to_thread`` 之后事件循环不再被文件 IO 卡住。

并发合并是顺带的好处：锁 + 代次计数让同时到达的多次 ``save`` 只写一次盘，
而每个 ``save`` 返回时都能保证自己的改动**已经在盘上**（``_written >= target``
检查）——不能返回了却还没落盘，用户是直接关窗口的。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from app.storage.memory import MemorySessionStore
from app.storage.models import ChatSession

logger = logging.getLogger(__name__)

_FORMAT_VERSION = 1


class FileSessionStore(MemorySessionStore):
    """会话持久化到单个 JSON 文件。启动时全量载入，写入时原子落盘。"""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        # 会话 id → 上次序列化出来的 JSON 片段。改一个会话不用把其余几百个重新序列化
        self._frozen: dict[str, str] = {}
        # 自上次成功落盘以来变过的会话 id
        self._stale: set[str] = set()
        self._flush_lock = asyncio.Lock()
        # 「第几次变更」与「已经写到第几次」。并发的多次 save 靠这两个数合并成一次写，
        # 同时保证每个 save 返回时自己的改动已经落盘
        self._generation = 0
        self._written = 0
        self._load()

    def _load(self) -> None:
        # 刚读进来的内容和磁盘一致，没有任何缓存可用
        self._frozen.clear()
        self._stale.clear()
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 读坏了不能让程序起不来：改名留证，从空库继续。
            # 直接删掉才是真的把用户数据弄丢了。
            broken = self._path.with_suffix(".broken.json")
            logger.error("会话文件损坏，已另存为 %s：%s", broken, exc)
            with contextlib.suppress(OSError):
                os.replace(self._path, broken)
            return

        items = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            try:
                session = ChatSession.from_dict(item)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("跳过一条无法解析的会话记录：%s", exc)
                continue
            # 直接塞进父类的字典，跳过 create() 的深拷贝——刚解析出来的对象没人共享
            self._data[session.id] = session
        logger.info("会话存储：文件（%s，%d 条）", self._path, len(self._data))

    def _render(self, pairs: list[tuple[str, ChatSession]], stale: set[str]) -> str:
        """拼出整个文件的 JSON 文本。**在线程里跑**，只重新序列化变过的会话。"""
        parts: list[str] = []
        for session_id, session in pairs:
            cached = self._frozen.get(session_id)
            if cached is None or session_id in stale:
                cached = json.dumps(session.to_dict(), ensure_ascii=False)
                self._frozen[session_id] = cached
            parts.append(cached)

        # 删掉的会话不能留在缓存里，否则长期运行会一直涨
        alive = {session_id for session_id, _ in pairs}
        self._frozen = {key: value for key, value in self._frozen.items() if key in alive}
        return f'{{"version":{_FORMAT_VERSION},"sessions":[{",".join(parts)}]}}'

    def _write_sync(self, text: str) -> bool:
        """原子写。**在线程里跑**。失败只记日志，不往上抛——内存里还有一份。"""
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8", newline="\n")
            os.replace(tmp, self._path)
        except OSError as exc:
            # 落盘失败不该让这一轮对话崩掉：内存里还有，用户这次能继续用
            logger.error("会话落盘失败（内存中的记录仍然可用）：%s", exc)
            return False
        return True

    async def _flush(self) -> None:
        """把当前状态写到盘上；返回时保证这次调用之前的改动已经在盘上。"""
        self._generation += 1
        target = self._generation

        async with self._flush_lock:
            if self._written >= target:
                # 等锁期间别的协程已经连着我的改动一起写过了
                return
            generation = self._generation
            # 快照必须在锁内取：锁外取的话，渲染期间新加的会话会漏进这次写，
            # 而它的缓存又没更新，下次就会被当成「没变过」直接复用旧内容
            pairs = list(self._data.items())
            stale = set(self._stale)

            text = await asyncio.to_thread(self._render, pairs, stale)
            if await asyncio.to_thread(self._write_sync, text):
                self._stale -= stale
                self._written = generation

    async def create(self, session: ChatSession) -> None:
        await super().create(session)
        self._stale.add(session.id)
        await self._flush()

    async def save(self, session: ChatSession) -> None:
        await super().save(session)
        self._stale.add(session.id)
        await self._flush()

    async def delete(self, session_id: str) -> None:
        await super().delete(session_id)
        await self._flush()

    async def aclose(self) -> None:
        """退出前兜底：上一次落盘失败过的话，这里再试一次。"""
        if self._stale:
            await self._flush()
