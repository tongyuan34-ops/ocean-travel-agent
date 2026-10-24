"""SSE 事件流缓冲区：生成与连接解耦，支持 Last-Event-ID 续传。

- 每个回答对应一个 StreamState，事件带自增 seq；
- 订阅者断开只取消订阅，不会取消后台生成；
- 重连时从 last_event_id 之后继续推送；
- 状态只放内存：进程重启后无法续传，但已完成的回答仍会落库。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamState:
    stream_id: str
    session_id: str
    events: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    updated_at: float = field(default_factory=time.time)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None

    async def emit(self, payload: dict[str, Any]) -> int:
        async with self.condition:
            seq = len(self.events) + 1
            self.events.append((seq, payload))
            self.updated_at = time.time()
            self.condition.notify_all()
            return seq

    async def finish(self) -> None:
        async with self.condition:
            self.done = True
            self.updated_at = time.time()
            self.condition.notify_all()
