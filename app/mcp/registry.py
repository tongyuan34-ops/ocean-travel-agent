"""外部 MCP 工具注册表：把远端工具接进本地的工具清单。

**为什么需要一层注册表，而不是直接改 ``ALL_TOOLS``**

``ALL_TOOLS`` 是模块级常量，被工具清单接口、Agent 构图、评测三处引用。
外部工具是**运行时**才知道的（要连子进程、要 ``tools/list``），而且可能一个都没有。
所以保留常量作为「内置工具」，外面套一层 ``all_tools()`` 返回「内置 + 外部」。

**失败必须吞掉**

MCP 服务端是可选依赖：命令写错、包没装、进程秒退，全都是现实里会发生的事。
这些情况下正确的行为是「少几个工具，应用照常起来」，而不是整个服务启动失败。
所以这里每一条错误路径都只记日志，不往上抛。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from app.agent.tools import ALL_TOOLS
from app.mcp.client import MCPError, StdioMCPClient, to_langchain_tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerSpec:
    """一个外部 MCP 服务端的启动方式。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


_clients: list[StdioMCPClient] = []
_external: list[BaseTool] = []


def parse_specs(raw: str) -> list[ServerSpec]:
    """解析 ``MCP_SERVERS``。

    格式故意跟 Claude Desktop 的 ``mcpServers`` 一致，用户可以把配置直接抄过来：

        [{"name": "fs", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}]

    坏一条只跳过那一条——一条写错不该让其余服务端都连不上。
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("MCP_SERVERS 不是合法 JSON，已忽略全部外部服务端：%s", exc)
        return []

    if isinstance(payload, dict):
        # 兼容 Claude Desktop 的形状：{"mcpServers": {"名字": {...}}}。
        # 这里的键就是服务端名字，所以补进配置里，否则日志里全是 server0
        inner = payload.get("mcpServers")
        if inner is None:
            inner = payload.get("servers")
        if isinstance(inner, dict):
            payload = [
                {**config, "name": config.get("name") or name}
                for name, config in inner.items()
                if isinstance(config, dict)
            ]
        elif isinstance(inner, list):
            payload = inner
        else:
            payload = []
    if not isinstance(payload, list):
        logger.error("MCP_SERVERS 必须是数组，实际是 %s", type(payload).__name__)
        return []

    specs: list[ServerSpec] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            logger.warning("跳过 MCP_SERVERS[%d]：不是对象", index)
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            logger.warning("跳过 MCP_SERVERS[%d]：缺少 command", index)
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            name = f"server{index}"
        args = item.get("args")
        env = item.get("env")
        specs.append(
            ServerSpec(
                name=name.strip(),
                command=command.strip(),
                args=tuple(str(arg) for arg in args) if isinstance(args, list) else (),
                env={str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
            )
        )
    return specs


async def connect(
    specs: list[ServerSpec],
    *,
    call_timeout: float = 120.0,
) -> list[BaseTool]:
    """连上每个服务端，把它的工具转成本地工具。

    返回成功注册的工具；失败的只记日志。重复调用会先断开旧的，
    避免同一份配置热重载时攒出两倍子进程。
    """
    await shutdown()
    registered: list[BaseTool] = []

    for spec in specs:
        client = StdioMCPClient(
            spec.name,
            spec.command,
            list(spec.args),
            env=spec.env,
            call_timeout=call_timeout,
        )
        try:
            await client.start()
            descriptors = await client.list_tools()
        except MCPError as exc:
            logger.error("MCP 服务端 %s 连接失败，跳过：%s", spec.name, exc)
            await client.close()
            continue
        except Exception as exc:
            logger.exception("MCP 服务端 %s 出现未预期错误，跳过：%s", spec.name, exc)
            await client.close()
            continue

        tools = [to_langchain_tool(client, item) for item in descriptors]
        _clients.append(client)
        registered.extend(tools)
        logger.info(
            "MCP 服务端 %s 已接入 %d 个工具：%s", spec.name, len(tools), [t.name for t in tools]
        )

    _external.clear()
    _external.extend(registered)
    return list(_external)


async def load_from_settings(settings: Any) -> int:
    """按配置连接外部服务端，返回注册的工具数。空配置直接返回 0。"""
    specs = parse_specs(getattr(settings, "mcp_servers", ""))
    if not specs:
        return 0
    tools = await connect(specs, call_timeout=float(getattr(settings, "mcp_call_timeout_sec", 120)))
    return len(tools)


async def shutdown() -> None:
    """断开所有外部服务端。进程退出前调用，否则子进程会变成孤儿。"""
    global _clients
    for client in _clients:
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不该影响退出
            logger.warning("关闭 MCP 服务端 %s 时出错：%s", client.name, exc)
    _clients = []
    _external.clear()


def external_tools() -> list[BaseTool]:
    return list(_external)


def all_tools() -> list[BaseTool]:
    """内置工具 + 外部工具。

    重名时保留内置的：外部工具名字里已经带了 ``mcp__server__`` 前缀，
    真撞上说明前缀规则出了问题，这时候让本地工具赢更安全。
    """
    seen = {tool.name for tool in ALL_TOOLS}
    merged = list(ALL_TOOLS)
    for tool in _external:
        if tool.name in seen:
            logger.warning("外部工具 %s 与内置工具重名，已忽略", tool.name)
            continue
        seen.add(tool.name)
        merged.append(tool)
    return merged
