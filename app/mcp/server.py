"""MCP 服务端：把现有的工具注册表暴露给任何 MCP 客户端。

**能直接跑起来的验证方式**

Claude Desktop / Cursor 的 MCP 配置里写：

    {"mcpServers": {"ocean": {"command": "python", "args": ["-m", "app.mcp.server"]}}}

或者手动喂一行 JSON：

    echo '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{...}}}' | python -m app.mcp.server

**为什么不暴露写操作**

文件写、删、换壁纸这些工具在应用内依赖 ``PermissionBroker``——它把确认请求推给
前端 SSE，等用户点「允许」。MCP 场景里没有那个前端，``require_permission`` 找不到
裁决通道就一律拒绝（这是刻意的失败方向）。于是把这些工具挂上 MCP 只有一个结果：
客户端看到一堆工具，调了必然 ``PERMISSION_DENIED``。

「能列出但永远失败」比「不列出」更糟——客户端会反复尝试，用户看到的是这个服务
坏了。所以这里按 ``DANGEROUS_TOOLS`` 直接过滤掉，并在 ``instructions`` 里说明。
要暴露它们得先解决「stdio 场景下谁来裁决」这个问题，不是加个开关就行的。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from langchain_core.tools import BaseTool

from app.agent.guard import DANGEROUS_TOOLS
from app.agent.tools import ALL_TOOLS
from app.core.errors import TOOL_ERROR_PREFIXES
from app.mcp import protocol
from app.mcp.protocol import ProtocolError

logger = logging.getLogger(__name__)

SERVER_NAME = "ocean-travel-agent"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "旅行规划 Agent 的工具集：天气、路线、POI、预算、住宿、交通、生图、网页检索、"
    "行程知识库检索。文件写入/删除类工具不在本服务端暴露——它们需要交互式确认，"
    "而 stdio 场景没有确认通道。"
)


def _input_schema(tool: BaseTool) -> dict[str, Any]:
    """取工具的入参 JSON Schema。

    ``inputSchema`` 在规范里是必填且**不能为 null**，所以拿不到 schema 时
    必须退化成一个合法的空对象 schema，而不是把 None 发出去。
    """
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        try:
            schema = args_schema.model_json_schema()
        except Exception:  # noqa: BLE001 - schema 生成失败不该让整个列表挂掉
            logger.warning("工具 %s 的 args_schema 生成失败，退化为空 schema", tool.name)
        else:
            if isinstance(schema, dict) and schema.get("type"):
                return schema
    return {"type": "object", "additionalProperties": False}


def _description(tool: BaseTool) -> str:
    return (getattr(tool, "description", "") or "").strip()


def _tool_descriptor(tool: BaseTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": _description(tool),
        "inputSchema": _input_schema(tool),
    }


def _as_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, (dict, list)):
        return json.dumps(output, ensure_ascii=False)
    return str(output)


class MCPServer:
    """双时代 MCP 服务端。

    ``_legacy`` 是唯一的状态，而且只在收到 ``initialize`` 时置位：
    无状态协议下没有别的跨请求状态可存，也不该存。
    """

    def __init__(
        self,
        tools: list[BaseTool] | None = None,
        *,
        name: str = SERVER_NAME,
        version: str = SERVER_VERSION,
        instructions: str = INSTRUCTIONS,
    ) -> None:
        exposed = tools if tools is not None else default_tools()
        self._tools = {tool.name: tool for tool in exposed}
        self._name = name
        self._version = version
        self._instructions = instructions
        self._legacy = False

    # ---------- 协议入口 ----------

    async def handle_line(self, line: str) -> str | None:
        """处理一行输入，返回要写回的一行；通知类返回 None。

        单行内的所有异常都收敛成 JSON-RPC 错误——stdio 是长连接，
        一行炸了不该让进程退出，否则客户端只会看到「服务端挂了」。
        """
        line = line.strip()
        if not line:
            return None
        try:
            message = protocol.decode(line)
        except ProtocolError as exc:
            return protocol.encode(exc.to_message(None))
        response = await self.handle(message)
        return None if response is None else protocol.encode(response)

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return protocol.error_message(request_id, protocol.INVALID_PARAMS, "params 必须是对象")
        if not isinstance(method, str):
            return protocol.error_message(request_id, protocol.INVALID_REQUEST, "缺少 method")

        # 通知没有 id，规范要求不得回包
        if "id" not in message:
            return None

        try:
            self._check_meta(method, params)
            result = await self._dispatch(method, params)
        except ProtocolError as exc:
            return exc.to_message(request_id)
        except Exception:
            logger.exception("处理 %s 时出错", method)
            return protocol.error_message(request_id, protocol.INTERNAL_ERROR, "服务端内部错误")
        return protocol.result_message(request_id, result)

    def _check_meta(self, method: str, params: dict[str, Any]) -> None:
        """现代请求必须带元数据；legacy 会话（已 initialize）不要求。"""
        meta = protocol.parse_meta(params)
        if meta is not None:
            protocol.require_supported(meta.protocol_version)
            return
        if method == "initialize":
            return
        if self._legacy:
            return
        raise ProtocolError(
            protocol.INVALID_PARAMS,
            "缺少必需的请求元数据",
            {"missing": [protocol.META_VERSION, protocol.META_CLIENT_CAPABILITIES]},
        )

    # ---------- 方法分发 ----------

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "server/discover":
            return self._discover()
        if method == "tools/list":
            return self._list_tools()
        if method == "tools/call":
            return await self._call_tool(params)
        if method == "ping":
            return {}
        raise ProtocolError(protocol.METHOD_NOT_FOUND, f"未实现的方法：{method}")

    def _server_info(self) -> dict[str, str]:
        return {"name": self._name, "version": self._version}

    def _capabilities(self) -> dict[str, Any]:
        return {"tools": {}}

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """legacy 握手。协商失败要明确报版本，否则老客户端只能瞎猜。"""
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested.strip():
            raise ProtocolError(protocol.INVALID_PARAMS, "initialize 缺少 protocolVersion")
        if requested not in protocol.SUPPORTED_VERSIONS:
            # legacy 客户端没有「降级」这条路，所以这里把它能用的版本全给出去
            raise ProtocolError(
                protocol.UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(protocol.SUPPORTED_VERSIONS), "requested": requested},
            )
        self._legacy = True
        return {
            "protocolVersion": requested,
            "capabilities": self._capabilities(),
            "serverInfo": self._server_info(),
            "instructions": self._instructions,
        }

    def _discover(self) -> dict[str, Any]:
        return {
            "resultType": protocol.RESULT_COMPLETE,
            "supportedVersions": list(protocol.SUPPORTED_VERSIONS),
            "capabilities": self._capabilities(),
            "_meta": {protocol.META_SERVER_INFO: self._server_info()},
            "instructions": self._instructions,
        }

    def _list_tools(self) -> dict[str, Any]:
        return {
            "resultType": protocol.RESULT_COMPLETE,
            "tools": [_tool_descriptor(tool) for tool in self._tools.values()],
        }

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(protocol.INVALID_PARAMS, "tools/call 缺少 name")
        tool = self._tools.get(name)
        if tool is None:
            # 未知工具是协议错误（规范示例就是 -32602），不是 isError
            raise ProtocolError(protocol.INVALID_PARAMS, f"Unknown tool: {name}")

        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(protocol.INVALID_PARAMS, "arguments 必须是对象")

        try:
            output = await tool.ainvoke(arguments)
        except Exception as exc:  # noqa: BLE001 - 工具异常要变成 isError，模型能看见
            logger.warning("工具 %s 执行异常：%s", name, exc)
            return protocol.call_result(f"TOOL_FAILED：{type(exc).__name__}: {exc}", is_error=True)

        text = _as_text(output)
        # 工具用错误码前缀表达业务失败，这里映射成 isError，模型才会去纠正
        return protocol.call_result(text, is_error=text.startswith(TOOL_ERROR_PREFIXES))


def default_tools() -> list[BaseTool]:
    """默认暴露的工具：全部只读工具。

    过滤依据是注入防护里那份 ``DANGEROUS_TOOLS``，不另写一份——
    两份清单漂移的后果是「这层以为只读、那层其实会写盘」。
    """
    return [tool for tool in ALL_TOOLS if tool.name not in DANGEROUS_TOOLS]


async def serve_stdio(
    server: MCPServer | None = None,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> None:
    """stdio 主循环。

    规范的三条硬要求：一条消息一行、stdout 只能写 MCP 消息、stdin 关闭就退出。
    读用线程池包 ``readline``——Windows 上 asyncio 没有可用的 stdin 管道，
    这是唯一跨平台且不引入依赖的做法。
    """
    instance = server if server is not None else MCPServer()
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    loop = asyncio.get_running_loop()

    while True:
        line = await loop.run_in_executor(None, input_stream.readline)
        if not line:
            logger.info("stdin 已关闭，退出")
            return
        response = await instance.handle_line(line)
        if response is None:
            continue
        output_stream.write(response + "\n")
        output_stream.flush()


def main() -> int:
    """``python -m app.mcp.server``：日志必须走 stderr，stdout 只留协议消息。"""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(serve_stdio())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
