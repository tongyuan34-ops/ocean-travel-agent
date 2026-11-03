"""MCP 客户端：真的起子进程，走真的 stdio。

这些用例必须真起进程，因为要验证的四条（排空 stderr、按 id 关联、探测年代、
超时后取消）全在进程边界上。用假对象测等于把要测的东西 mock 掉了。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.mcp.client import MCPError, StdioMCPClient, to_langchain_tool
from app.mcp.registry import ServerSpec, all_tools, connect, parse_specs, shutdown

FAKE = Path(__file__).resolve().parent / "fake_mcp_server.py"


def _client(mode: str, **kwargs) -> StdioMCPClient:
    return StdioMCPClient(
        "fake",
        sys.executable,
        [str(FAKE), "--mode", mode],
        probe_timeout=kwargs.pop("probe_timeout", 20.0),
        call_timeout=kwargs.pop("call_timeout", 20.0),
        **kwargs,
    )


# ---------- 年代探测 ----------


async def test_modern_server_is_detected():
    client = _client("modern")
    try:
        await client.start()
        assert client._era == "modern"
        assert client._version == "2026-07-28"
    finally:
        await client.close()


async def test_legacy_server_falls_back_to_initialize():
    """老服务端不认识 server/discover，客户端要退回握手而不是直接失败。

    这条最容易写错：如果只认某个特定错误码，或者把「任何错误」当致命错误，
    表现就是「连上了但一个工具都列不出来」。
    """
    client = _client("legacy")
    try:
        await client.start()
        assert client._era == "legacy"
        # 握手时先要最高的老版本（2025-11-25），假服务端原样回显
        assert client._version == "2025-11-25"
    finally:
        await client.close()


async def test_silent_server_times_out_with_diagnostics():
    client = _client("silent", probe_timeout=0.4)
    try:
        with pytest.raises(MCPError) as exc:
            await client.start()
        assert "没有响应" in str(exc.value) or "无法启动" in str(exc.value)
    finally:
        await client.close()


# ---------- 工具调用 ----------


async def test_list_tools_reads_descriptors():
    client = _client("modern")
    try:
        await client.start()
        tools = await client.list_tools()
        assert {item["name"] for item in tools} == {"echo", "fail", "boom", "noisy"}
        assert tools[0]["inputSchema"]["type"] == "object"
    finally:
        await client.close()


async def test_call_tool_returns_content():
    client = _client("modern")
    try:
        await client.start()
        result = await client.call_tool("echo", {"text": "湄洲岛"})
        assert result["content"][0]["text"] == "湄洲岛"
    finally:
        await client.close()


async def test_unknown_tool_raises_with_code():
    client = _client("modern")
    try:
        await client.start()
        with pytest.raises(MCPError) as exc:
            await client.call_tool("nope", {})
        assert exc.value.code == -32602
    finally:
        await client.close()


async def test_large_stderr_does_not_deadlock():
    """子进程往 stderr 灌满缓冲区时必须还能收到响应。

    不排空 stderr 的话，子进程会阻塞在写日志上，客户端表现是「请求发出去
    永远没回」——而串行测试里根本看不出来。
    """
    client = _client("modern")
    try:
        await client.start()
        result = await client.call_tool("noisy", {"lines": 2000})
        assert result["content"][0]["text"] == "ok"
    finally:
        await client.close()


# ---------- 转成 LangChain 工具 ----------


async def test_conversion_prefixes_name_and_calls_through():
    client = _client("modern")
    try:
        await client.start()
        tools = {item["name"]: item for item in await client.list_tools()}
        local = to_langchain_tool(client, tools["echo"])

        assert local.name == "mcp__fake__echo"
        assert "MCP 服务端 fake" in local.description
        assert await local.ainvoke({"text": "hi"}) == "hi"
    finally:
        await client.close()


async def test_conversion_marks_external_failure_with_prefix():
    """外部工具说失败时要加错误码前缀，上层才能和本地工具走同一套判定。"""
    client = _client("modern")
    try:
        await client.start()
        tools = {item["name"]: item for item in await client.list_tools()}
        local = to_langchain_tool(client, tools["fail"])

        output = await local.ainvoke({})
        assert output.startswith("MCP_TOOL_FAILED")
    finally:
        await client.close()


async def test_conversion_survives_bad_schema():
    """inputSchema 缺 properties 时不能崩，退化成不校验即可。"""
    client = _client("modern")
    try:
        await client.start()
        local = to_langchain_tool(
            client, {"name": "weird-name.v2", "description": "", "inputSchema": {"type": "object"}}
        )
        # 名字里的点保留（MCP 允许），构造不崩就够了；
        # 参数模型退化成空模型，调用时原样透传
        assert local.name == "mcp__fake__weird-name.v2"
        assert local.args_schema is not None
    finally:
        await client.close()


# ---------- 注册表 ----------


def test_parse_specs_accepts_claude_desktop_shape():
    raw = '{"mcpServers": {"fs": {"command": "npx", "args": ["-y", "pkg"]}}}'
    specs = parse_specs(raw)

    # 字典形态没有 name 字段时按序号兜底，不能整批丢掉
    assert len(specs) == 1
    assert specs[0].command == "npx"
    assert specs[0].args == ("-y", "pkg")


def test_parse_specs_skips_broken_entries():
    raw = '[{"name":"a","command":"x"},{"name":"b"},{"command":"c","name":"c"}]'
    specs = parse_specs(raw)

    assert [spec.name for spec in specs] == ["a", "c"]


def test_parse_specs_handles_garbage():
    assert parse_specs("") == []
    assert parse_specs("{不是 json") == []
    assert parse_specs('"字符串"') == []


async def test_connect_registers_tools_and_merges_with_builtin():
    specs = [ServerSpec(name="fake", command=sys.executable, args=(str(FAKE), "--mode", "modern"))]
    try:
        tools = await connect(specs)
        names = {item.name for item in tools}
        assert "mcp__fake__echo" in names

        merged = {item.name for item in all_tools()}
        assert "mcp__fake__echo" in merged
        # 内置工具还在
        assert "generate_image" in merged
    finally:
        await shutdown()


async def test_broken_server_does_not_raise():
    """命令写错只记日志：可选集成不该让应用起不来。"""
    specs = [ServerSpec(name="bad", command="definitely-not-a-command-xyz")]
    try:
        tools = await connect(specs)
        assert tools == []
    finally:
        await shutdown()
