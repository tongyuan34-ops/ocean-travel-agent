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

**写入策略**

每次 ``save`` 都整份落盘。会话是几十 KB 量级，一次 JSON 序列化不到 1 毫秒，
不值得为它引入增量写或写队列。但**必须原子**：先写 ``.tmp`` 再 ``os.replace``，
否则断电或崩溃会留下一个半截的 JSON，下次启动直接读不出来，用户看到的是
「所有记录都没了」——比慢一点严重得多。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

from app.storage.memory import MemorySessionStore
from app.storage.models import ChatSession

logger = logging.getLogger(__name__)


class FileSessionStore(MemorySessionStore):
    """会话持久化到单个 JSON 文件。启动时全量载入，写入时全量落盘。"""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self._load()

    def _load(self) -> None:
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

    def _flush(self) -> None:
        payload = {
            "version": 1,
            "sessions": [s.to_dict() for s in self._data.values()],
        }
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
                newline="\n",
            )
            os.replace(tmp, self._path)
        except OSError as exc:
            # 落盘失败不该让这一轮对话崩掉：内存里还有，用户这次能继续用
            logger.error("会话落盘失败（内存中的记录仍然可用）：%s", exc)

    async def create(self, session: ChatSession) -> None:
        await super().create(session)
        self._flush()

    async def save(self, session: ChatSession) -> None:
        await super().save(session)
        self._flush()

    async def delete(self, session_id: str) -> None:
        await super().delete(session_id)
        self._flush()
