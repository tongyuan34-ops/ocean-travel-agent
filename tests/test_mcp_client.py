"""MCP 客户端：真的起子进程，走真的 stdio。

这些用例必须真起进程，因为要验证的四条（排空 stderr、按 id 关联、探测年代、
超时后取消）全在进程边界上。用假对象测等于把要测的东西 mock 掉了。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from app.mcp.client import MCPError, StdioMCPClient, _result_text, to_langchain_tool
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


async def test_external_failure_is_recognised_as_error_by_sse_layer():
    """外部工具失败必须被 SSE 的 tool_result 记成 error，而不是 ok。

    这里真漏过一次：``MCP_TOOL_FAILED`` 只写在 client.py 的返回值里，
    没进 ``TOOL_ERROR_PREFIXES``。后果是前端把一次失败渲染成绿色成功，
    而模型拿到的是失败文本——两边对同一件事的判断不一致。
    """
    from app.core.errors import TOOL_ERROR_PREFIXES

    client = _client("modern")
    try:
        await client.start()
        tools = {item["name"]: item for item in await client.list_tools()}
        output = await to_langchain_tool(client, tools["fail"]).ainvoke({})
        assert output.startswith(TOOL_ERROR_PREFIXES)
    finally:
        await client.close()


# ---------- 子进程中途死掉 ----------


async def test_crash_mid_call_fails_fast_instead_of_waiting_for_timeout():
    """服务端在 tools/call 里崩掉，报错必须是「已退出」而不是「超时」。

    之前 stdout 读到 EOF 就直接 return，挂着的 future 没人管，调用方要等满
    ``call_timeout``（线上是 120s）才拿到一句「在 120s 内没有响应」——
    真因是崩溃，报出来的是超时，排障方向直接跑偏。
    """
    client = _client("crash", call_timeout=20.0)
    try:
        await client.start()
        await client.list_tools()
        started = time.monotonic()
        with pytest.raises(MCPError) as excinfo:
            await client.call_tool("echo", {"text": "x"})
        elapsed = time.monotonic() - started
        assert "已退出" in str(excinfo.value)
        # 远小于 call_timeout 就说明是 EOF 立刻失败的，不是等超时
        assert elapsed < 5.0
    finally:
        await client.close()


async def test_crash_clears_state_so_next_call_can_reconnect():
    """崩过一次之后状态要清干净，否则后续调用会一直撞在同一个死进程上。"""
    client = _client("crash", call_timeout=20.0)
    try:
        await client.start()
        await client.list_tools()
        with pytest.raises(MCPError):
            await client.call_tool("echo", {"text": "x"})
        # 等 EOF 任务跑完
        await asyncio.sleep(0)
        assert client._proc is None
        assert client._era is None
    finally:
        await client.close()


# ---------- 服务端反过来发请求 ----------


async def _wait_for_replies(client: StdioMCPClient, expected: int) -> list[str]:
    """等子进程往 stderr 打出足够多的「客户端回复」记录。"""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        replies = [line for line in client._stderr_tail if line.startswith("CLIENT_REPLIED")]
        if len(replies) >= expected:
            return replies
        await asyncio.sleep(0.02)
    return [line for line in client._stderr_tail if line.startswith("CLIENT_REPLIED")]


async def test_client_answers_server_initiated_requests():
    """服务端主动发来的请求必须回，否则对端会一直等到自己的超时。

    ping 是双向的，回空结果；我们没声明 roots 能力，所以 roots/list 回 -32601
    让服务端自己降级——装死比报错更糟，对端分不清「慢」和「不支持」。
    """
    client = _client("chatty")
    try:
        await client.start()
        await client.list_tools()
        replies = await _wait_for_replies(client, 2)

        assert any('"id":9001' in line and '"result"' in line for line in replies), replies
        assert any('"id":9002' in line and "-32601" in line for line in replies), replies
    finally:
        await client.close()


# ---------- 非文本内容块 ----------


def test_image_only_result_is_not_an_empty_string():
    """服务端只返回一张图时，压成文本不能是空串。

    空串的后果是模型看到「工具成功、返回空」，既不知道发生了什么也无从纠正。
    """
    text = _result_text(
        {"content": [{"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"}]}
    )

    assert text
    assert "图片" in text
    # base64 不能漏进上下文
    assert "aGVsbG8=" not in text


def test_mixed_result_keeps_text_and_notes_what_was_skipped():
    text = _result_text(
        {
            "content": [
                {"type": "text", "text": "前一句"},
                {"type": "image", "data": "x"},
                {"type": "audio", "data": "y"},
            ]
        }
    )

    assert text.startswith("前一句")
    assert "2 个非文本内容块" in text
    assert "图片" in text and "音频" in text


def test_empty_text_block_stays_empty():
    """纯空文本不该凭空造出一句占位——那是服务端真的没话说。"""
    assert _result_text({"content": [{"type": "text", "text": ""}]}) == ""
