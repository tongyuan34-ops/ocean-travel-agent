"""MCP 服务端：双时代语义、工具暴露策略、错误映射。

这个服务端的价值全在「客户端能不能用」上，所以用例都从协议报文进、从报文出，
不直接调内部方法。
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Any

from langchain_core.tools import tool

from app.agent.guard import DANGEROUS_TOOLS
from app.mcp.server import MCPServer, serve_stdio

META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@tool
def echo(text: str) -> str:
    """原样返回。"""
    return text


@tool
def broken(text: str) -> str:
    """抛异常。"""
    raise RuntimeError("炸了")


@tool
def business_error(text: str) -> str:
    """返回错误码前缀。"""
    return "TOOL_NOT_ENABLED：没配 Key"


def _server() -> MCPServer:
    return MCPServer(tools=[echo, broken, business_error])


async def _call(server: MCPServer, method: str, params: dict[str, Any] | None = None) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {"_meta": META, **(params or {})},
    }
    raw = await server.handle_line(json.dumps(payload, ensure_ascii=False))
    assert raw is not None
    return json.loads(raw)


# ---------- 现代语义 ----------


async def test_discover_reports_versions_and_identity():
    """server/discover 是规范要求 MUST 实现的，客户端靠它判断年代。"""
    payload = await _call(_server(), "server/discover")
    result = payload["result"]

    assert result["resultType"] == "complete"
    assert "2026-07-28" in result["supportedVersions"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"]


async def test_tools_list_always_has_schema():
    """inputSchema 在规范里必填且不能为 null，缺了客户端会解析失败。"""
    payload = await _call(_server(), "tools/list")

    for item in payload["result"]["tools"]:
        assert item["inputSchema"]
        assert item["inputSchema"]["type"] == "object"


async def test_request_without_meta_is_rejected_in_modern_mode():
    """无状态协议下缺元数据就是 malformed，必须 -32602。"""
    server = _server()
    raw = await server.handle_line('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
    payload = json.loads(raw)

    assert payload["error"]["code"] == -32602


async def test_unknown_version_gets_supported_list():
    server = _server()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": {**META, "io.modelcontextprotocol/protocolVersion": "1999-01-01"}},
    }
    raw = await server.handle_line(json.dumps(payload))
    error = json.loads(raw)["error"]

    assert error["code"] == -32022
    assert "2026-07-28" in error["data"]["supported"]


# ---------- legacy 语义 ----------


async def test_initialize_switches_to_legacy_and_allows_missing_meta():
    """收到 initialize 之后就是 legacy 会话，后续请求允许不带 _meta。"""
    server = _server()
    init = await server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
    )
    assert json.loads(init)["result"]["protocolVersion"] == "2025-06-18"

    listing = await server.handle_line('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
    assert "result" in json.loads(listing)


async def test_initialize_with_unknown_version_is_rejected():
    server = _server()
    raw = await server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1900-01-01"},
            }
        )
    )

    assert json.loads(raw)["error"]["code"] == -32022


# ---------- 工具调用 ----------


async def test_call_returns_text_content():
    payload = await _call(_server(), "tools/call", {"name": "echo", "arguments": {"text": "你好"}})
    result = payload["result"]

    assert result["isError"] is False
    assert result["content"][0] == {"type": "text", "text": "你好"}


async def test_unknown_tool_is_a_protocol_error():
    """未知工具是协议错误（-32602），不是 isError——模型看不到协议错误内容。"""
    payload = await _call(_server(), "tools/call", {"name": "不存在", "arguments": {}})

    assert payload["error"]["code"] == -32602


async def test_tool_exception_becomes_is_error_not_crash():
    """工具抛异常要变成 isError，否则一行报错就把整个 stdio 连接带走了。"""
    payload = await _call(_server(), "tools/call", {"name": "broken", "arguments": {"text": "x"}})
    result = payload["result"]

    assert result["isError"] is True
    assert "TOOL_FAILED" in result["content"][0]["text"]


async def test_error_code_prefix_maps_to_is_error():
    """工具用错误码前缀表达业务失败，服务端要把它翻译成 isError。"""
    payload = await _call(
        _server(), "tools/call", {"name": "business_error", "arguments": {"text": "x"}}
    )

    assert payload["result"]["isError"] is True


async def test_missing_arguments_defaults_to_empty():
    payload = await _call(_server(), "tools/call", {"name": "echo", "arguments": {}})

    assert "result" in payload


# ---------- 连接健壮性 ----------


async def test_notification_gets_no_reply():
    """没有 id 的是通知，规范要求不得回包；回了会让客户端把响应和请求错配。"""
    server = _server()
    raw = await server.handle_line(
        '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'
    )

    assert raw is None


async def test_bad_json_does_not_kill_the_connection():
    server = _server()
    raw = await server.handle_line("{不是 json")

    assert json.loads(raw)["error"]["code"] == -32700


async def test_blank_line_is_ignored():
    assert await _server().handle_line("   ") is None


async def test_unknown_method_gets_method_not_found():
    payload = await _call(_server(), "resources/list")

    assert payload["error"]["code"] == -32601


# ---------- 暴露策略 ----------


def test_write_tools_are_not_exposed():
    """写操作依赖前端裁决通道，stdio 场景没有，暴露出来只会永远失败。

    「能列出但必然失败」比「不列出」更糟：客户端会反复重试，用户以为服务坏了。
    """
    names = {tool.name for tool in _server()._tools.values()}
    assert not (names & DANGEROUS_TOOLS)


def test_default_server_exposes_read_only_tools():
    from app.agent.tools import ALL_TOOLS
    from app.mcp.server import default_tools

    names = {item.name for item in default_tools()}

    assert names
    assert not (names & DANGEROUS_TOOLS)
    assert names <= {item.name for item in ALL_TOOLS}


# ---------- 注解：客户端靠它决定要不要自动批准 ----------


@tool
def generate_image(prompt: str) -> str:
    """假装提交一个生图任务。"""
    return "task-1"


async def test_read_only_tools_are_marked_read_only():
    """``readOnlyHint`` 是客户端自动批准的依据，标错等于把有副作用的调用静默执行。"""
    payload = await _call(_server(), "tools/list")
    annotations = {item["name"]: item["annotations"] for item in payload["result"]["tools"]}

    assert annotations["echo"]["readOnlyHint"] is True
    assert annotations["echo"]["destructiveHint"] is False


async def test_tool_with_side_effects_is_not_marked_read_only():
    """生图会提交任务、占额度、产出文件——不在 DANGEROUS_TOOLS 里，但也不是只读。"""
    server = MCPServer(tools=[generate_image])
    payload = await _call(server, "tools/list")

    annotations = payload["result"]["tools"][0]["annotations"]
    assert annotations["readOnlyHint"] is False


# ---------- stdio 主循环：并发与取消 ----------


class _Stdin:
    """按行喂给 serve_stdio 的假 stdin。readline 在 executor 线程里跑，pop 够用。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _request(request_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    return _line(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {"_meta": META, **(params or {})},
        }
    )


async def test_slow_tool_does_not_block_other_requests():
    """串行处理时，一个慢工具会把后面的请求（包括 ping）全堵在队列里。

    客户端等不到 ping 回复会判定连接挂了，实际只是我们在慢慢跑另一个工具。
    """
    finished: list[str] = []

    @tool
    async def slow() -> str:
        """睡一会儿再返回。"""
        await asyncio.sleep(0.3)
        finished.append("slow")
        return "done"

    @tool
    async def fast() -> str:
        """立刻返回。"""
        finished.append("fast")
        return "quick"

    server = MCPServer(tools=[slow, fast])
    stdin = _Stdin(
        [
            _request(1, "tools/call", {"name": "slow", "arguments": {}}),
            _request(2, "tools/call", {"name": "fast", "arguments": {}}),
        ]
    )
    stdout = io.StringIO()

    await serve_stdio(server, stdin=stdin, stdout=stdout)

    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    order = [item["id"] for item in responses]
    # 后发的先回：说明两个请求是并发的，不是一个等一个
    assert order == [2, 1]
    assert finished == ["fast", "slow"]


async def test_cancelled_request_is_really_aborted():
    """收到 notifications/cancelled 要把工具**停掉**，而不只是不回复。

    只不回复的话，客户端以为不做了，服务端还在算——额度照烧。
    """
    completed: list[str] = []

    @tool
    async def hang() -> str:
        """永远不自己结束。"""
        await asyncio.sleep(5)
        completed.append("hang")
        return "never"

    server = MCPServer(tools=[hang])
    stdin = _Stdin(
        [
            _request(7, "tools/call", {"name": "hang", "arguments": {}}),
            _line(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 7},
                }
            ),
        ]
    )
    stdout = io.StringIO()

    started = time.monotonic()
    await serve_stdio(server, stdin=stdin, stdout=stdout)
    elapsed = time.monotonic() - started

    assert completed == []
    # 没有响应：被取消的请求不该产生回包
    assert stdout.getvalue().strip() == ""
    # 必须远小于 5 秒的 sleep，也小于收尾窗口（2 秒）——否则「取消」只是
    # 等收尾超时才被顺手杀掉，和真取消看起来一样，实际白跑了 2 秒
    assert elapsed < 1.0


async def test_cancel_for_unknown_request_is_ignored():
    """取消一个不存在（或已经结束）的请求不能炸。"""
    server = MCPServer(tools=[echo])
    stdin = _Stdin(
        [
            _line(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 999},
                }
            )
        ]
    )
    stdout = io.StringIO()

    await serve_stdio(server, stdin=stdin, stdout=stdout)

    assert stdout.getvalue() == ""
