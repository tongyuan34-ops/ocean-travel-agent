"""Redis 异步客户端工厂。

所有需要 Redis 的地方都从这里拿客户端，理由只有一个：**超时必须显式设**。

redis-py 8 把默认 socket 超时从「不超时」改成了 5 秒
（``DEFAULT_SOCKET_TIMEOUT = 5``）。本机实测第一次连接要 2 秒左右，冷启动时
「建连接 + 第一条命令」很容易一起越过 5 秒，于是重启后第一个请求直接超时——
SSE 那边表现成「200 但一个事件都没有」，会话存储那边表现成 500，都不好查。

这里统一放宽到 10 秒，并把连接超时与读超时分开写清楚：读超时不该被
连接超时连坐。不要在这一层加 ``retry_on_timeout``——Pub/Sub 的读是阻塞式的，
重试会把「这一轮确实没有消息」变成一次多余的往返。
"""

from __future__ import annotations

from typing import Any

#: 建连超时（含 DNS 与 TCP 握手）
CONNECT_TIMEOUT_SEC = 10.0
#: 单条命令的读超时
READ_TIMEOUT_SEC = 10.0


def async_redis(url: str) -> Any:
    """建一个异步客户端。**不会立刻连接**，真正连上发生在第一条命令时。"""
    from redis.asyncio import from_url

    return from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=CONNECT_TIMEOUT_SEC,
        socket_timeout=READ_TIMEOUT_SEC,
    )
