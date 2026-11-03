"""MCP 客户端：连外部 MCP 服务端，把它们的工具变成 Agent 能调的工具。

**stdio 客户端的四个坑，每个都能让程序「看起来正常但什么都不工作」**

1. **必须排空 stderr。** 子进程的 stderr 是管道，缓冲区满了（Linux 64KB /
   Windows 约 4KB）子进程会阻塞在写日志上，表现是「请求发出去永远没回」。
   所以起一个常驻任务一直读，只保留最后若干行用于报错。
2. **必须按 id 关联。** stdout 是单通道，响应、通知混在一起。不按 id 分发的话，
   并发两个请求就会互相拿错结果——而这种错在串行测试里永远不会暴露。
3. **必须探测协议年代。** 2026-07-28 是无状态（每请求带 ``_meta``），
   2025-11-25 及更早要先 ``initialize``。规范建议先发 ``server/discover``：
   拿到 ``DiscoverResult`` 说明是新版；拿到别的错误或超时说明是老版，退回握手。
   **不能只认某个特定错误码**——老服务端对未知方法的报错是实现自定的。
4. **超时后要发 ``notifications/cancelled``。** 否则服务端还在算，而我们已经
   放弃了；连开几轮就会攒下一堆僵尸计算。

没有引入官方 SDK：理由同 ``protocol`` 模块——协议本体很小，SDK 会带进一整层
传递依赖。代价是这里要自己处理上面四条，而它们恰好是最容易写错的地方。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections import deque
from typing import Any, Protocol

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from app.mcp import protocol

logger = logging.getLogger(__name__)

CLIENT_NAME = "ocean-travel-agent"
CLIENT_VERSION = "0.1.0"

# 探测/调用超时。探测给短一点：老服务端可能根本不回，等太久启动就被拖住
PROBE_TIMEOUT_SEC = 10.0
CALL_TIMEOUT_SEC = 120.0

# 保留多少行 stderr 用于报错诊断
_STDERR_TAIL = 40


class MCPError(Exception):
    """客户端侧错误：连不上、超时、服务端返回 error。

    ``code`` 保留服务端的 JSON-RPC 错误码：探测协议年代时必须靠它区分
    「新版但不支持我的版本」（-32022）和「老版不认识这个方法」（实现自定），
    拿错误文本去匹配字符串是脆的。
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MCPClient(Protocol):
    """外部服务端的抽象。测试里可以换成假实现，不必真起子进程。"""

    name: str

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    async def close(self) -> None: ...


def _resolve_command(command: str) -> str:
    """在 Windows 上 ``npx`` 是 ``npx.cmd``，直接 exec 会 FileNotFoundError。"""
    found = shutil.which(command)
    return found or command


class StdioMCPClient:
    """按 MCP stdio 传输协议启动并驱动一个子进程。"""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        probe_timeout: float = PROBE_TIMEOUT_SEC,
        call_timeout: float = CALL_TIMEOUT_SEC,
    ) -> None:
        self.name = name
        self._command = _resolve_command(command)
        self._args = list(args or [])
        self._env = {**os.environ, **(env or {})}
        self._cwd = cwd
        self._probe_timeout = probe_timeout
        self._call_timeout = call_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._era: str | None = None  # "modern" / "legacy"
        self._version = protocol.PROTOCOL_VERSION
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        self._tasks: list[asyncio.Task[None]] = []

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
            )
        except (OSError, NotImplementedError) as exc:
            raise MCPError(f"无法启动 MCP 服务端 {self.name}（{self._command}）：{exc}") from exc

        self._tasks = [
            asyncio.create_task(self._read_stdout(), name=f"mcp-{self.name}-stdout"),
            asyncio.create_task(self._read_stderr(), name=f"mcp-{self.name}-stderr"),
        ]
        await self._negotiate()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError("客户端已关闭"))
        self._pending.clear()

        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            # 规范：先关 stdin 等它自己退，超时才强杀
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except TimeoutError:
            logger.warning("MCP 服务端 %s 未在 3 秒内退出，强制结束", self.name)
            proc.kill()
            await proc.wait()

    # ---------- IO ----------

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = protocol.decode(text)
            except protocol.ProtocolError as exc:
                logger.warning(
                    "MCP %s 返回了非协议内容：%s（%s）", self.name, exc.message, text[:200]
                )
                continue

            request_id = message.get("id")
            future = self._pending.pop(request_id, None)
            if future is None:
                # 通知（progress / log）或无法关联的响应，忽略但不报错
                logger.debug("MCP %s 未关联消息：%s", self.name, text[:200])
                continue
            if not future.done():
                future.set_result(message)

    async def _read_stderr(self) -> None:
        """常驻读 stderr。不读的话子进程会卡在写日志上，请求永远收不到回。"""
        assert self._proc is not None and self._proc.stderr is not None
        stream = self._proc.stderr
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self._stderr_tail.append(text)
            logger.debug("MCP %s stderr: %s", self.name, text)

    def _meta(self) -> dict[str, Any]:
        return {
            protocol.META_VERSION: self._version,
            protocol.META_CLIENT_INFO: {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            protocol.META_CLIENT_CAPABILITIES: {},
        }

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
        modern: bool,
    ) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError(f"MCP 服务端 {self.name} 未启动")

        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        if modern:
            payload["params"]["_meta"] = self._meta()

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            proc.stdin.write((protocol.encode(payload) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise MCPError(f"MCP 服务端 {self.name} 已退出：{self._diagnostics()}") from exc

        try:
            message = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            await self._notify_cancelled(request_id, modern)
            raise MCPError(
                f"MCP 服务端 {self.name} 在 {timeout:.0f}s 内没有响应 {method}：{self._diagnostics()}"
            ) from exc

        if "error" in message:
            error = message.get("error") or {}
            code = error.get("code")
            raise MCPError(
                f"MCP 服务端 {self.name} 返回错误 {code}：{error.get('message')}",
                code=code if isinstance(code, int) else None,
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务端 {self.name} 的 {method} 结果不是对象")
        return result

    async def _notify_cancelled(self, request_id: Any, modern: bool) -> None:
        """放弃请求后告诉服务端别算了。发不出去也不影响主流程。"""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            return
        params: dict[str, Any] = {"requestId": request_id}
        if modern:
            params["_meta"] = self._meta()
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": params,
        }
        try:
            proc.stdin.write((protocol.encode(notification) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("MCP %s 取消失败（连接已断）", self.name)

    def _diagnostics(self) -> str:
        if not self._stderr_tail:
            return "没有 stderr 输出"
        return " | ".join(list(self._stderr_tail)[-5:])

    # ---------- 协议协商 ----------

    async def _negotiate(self) -> None:
        """先探测年代，再决定后续请求要不要带 ``_meta``。

        判定规则来自规范：``DiscoverResult`` → 新版；``UnsupportedProtocolVersionError``
        → 也是新版（只是版本不合，从它的 ``supported`` 里挑）；其他任何错误或超时
        → 老版，退回 ``initialize``。
        """
        try:
            result = await self._request(
                "server/discover", {}, timeout=self._probe_timeout, modern=True
            )
        except MCPError as exc:
            if exc.code == protocol.UNSUPPORTED_PROTOCOL_VERSION:
                # 新版但版本不认识：说明我们写死了它不支持的版本
                raise MCPError(
                    f"MCP 服务端 {self.name} 是 2026-07-28 之后的新版，但不支持 "
                    f"{protocol.PROTOCOL_VERSION}：{exc}"
                ) from exc
            logger.info("MCP %s 不支持 server/discover，按老版握手：%s", self.name, exc)
            await self._initialize_legacy()
            return

        supported = result.get("supportedVersions")
        if isinstance(supported, list) and supported:
            chosen = next(
                (v for v in supported if v in protocol.SUPPORTED_VERSIONS),
                None,
            )
            if chosen is None:
                raise MCPError(f"MCP 服务端 {self.name} 只支持 {supported}，本客户端都不支持")
            self._version = chosen
        self._era = "modern"
        logger.info("MCP %s 使用现代协议 %s", self.name, self._version)

    async def _initialize_legacy(self) -> None:
        # 老版里版本号是「我最高支持到哪」，服务端可以回一个更低的
        requested = protocol.LEGACY_VERSIONS[0]
        result = await self._request(
            "initialize",
            {
                "protocolVersion": requested,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            timeout=self._probe_timeout,
            modern=False,
        )
        version = result.get("protocolVersion")
        if isinstance(version, str) and version:
            self._version = version
        self._era = "legacy"

        # 握手后按规范补一个 initialized 通知；不带 id，不需要等回复
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            proc.stdin.write((protocol.encode(notification) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        logger.info("MCP %s 使用老版协议 %s（initialize 握手）", self.name, self._version)

    # ---------- 工具 ----------

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._era is None:
            await self.start()
        result = await self._request(
            "tools/list", {}, timeout=self._probe_timeout, modern=self._era != "legacy"
        )
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPError(f"MCP 服务端 {self.name} 的 tools/list 没有 tools 数组")
        return [item for item in tools if isinstance(item, dict) and item.get("name")]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._era is None:
            await self.start()
        return await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=self._call_timeout,
            modern=self._era != "legacy",
        )


# ---------- 把 MCP 工具转成 LangChain 工具 ----------

# JSON Schema 类型 → Python 类型。只覆盖工具入参常见的那几种，
# 遇到不认识的按 Any 处理——宁可宽一点，也不要因为某个奇怪 schema 整批注册失败
_SCALARS: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _python_type(schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    if kind in _SCALARS:
        return _SCALARS[kind]
    if kind == "array":
        item = schema.get("items")
        inner = _python_type(item) if isinstance(item, dict) else Any
        return list[inner]  # type: ignore[valid-type]
    if kind == "object":
        return dict[str, Any]
    if isinstance(kind, list):
        # 联合类型（如 ["string","null"]）取第一个能识别的
        for candidate in kind:
            if candidate in _SCALARS:
                return _SCALARS[candidate]
    return Any


def _args_model(tool_name: str, schema: dict[str, Any]) -> type[BaseModel] | None:
    """把 inputSchema 转成 pydantic 模型，LangChain 才能校验参数。

    转不出来就返回 None，调用方退化成「不校验、原样透传」——
    工具名里带连字符时类名不合法，这种细节不该让整个工具注册失败。
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None

    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        annotation = _python_type(spec)
        description = spec.get("description") or ""
        if key in required:
            fields[key] = (annotation, Field(..., description=description))
        else:
            default = spec.get("default")
            fields[key] = (annotation, Field(default, description=description))

    model_name = "".join(ch if ch.isalnum() else "_" for ch in tool_name) or "Args"
    try:
        return create_model(f"{model_name}Args", **fields)
    except Exception:  # noqa: BLE001 - 生成失败就退化成不校验
        logger.warning("MCP 工具 %s 的参数模型生成失败，退化为不校验", tool_name)
        return None


def _qualified(server: str, tool: str) -> str:
    """加前缀避免和本地工具重名，同时保留来源，排障时一眼看出是谁的。"""
    raw = f"mcp__{server}__{tool}"
    return "".join(ch if (ch.isalnum() or ch in "_-.") else "_" for ch in raw)


def to_langchain_tool(
    client: MCPClient,
    descriptor: dict[str, Any],
) -> StructuredTool:
    """把一个 MCP 工具描述变成可被 Agent 调用的工具。

    返回文本的约定与本地工具一致：失败以错误码开头（``MCP_TOOL_FAILED：...``），
    这样上层的 tool_result status 判定不用为外部工具写特例。
    """
    remote_name = str(descriptor["name"])
    local_name = _qualified(client.name, remote_name)
    schema = descriptor.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "additionalProperties": False}
    args_model = _args_model(local_name, schema)
    description = (descriptor.get("description") or "").strip() or f"MCP 工具 {remote_name}"

    async def _call(**kwargs: Any) -> str:
        try:
            result = await client.call_tool(remote_name, kwargs)
        except MCPError as exc:
            return f"MCP_TOOL_FAILED：{exc}"

        text = _result_text(result)
        if result.get("isError"):
            # 外部工具自己说的失败，前缀标清楚来源，模型据此纠正
            return f"MCP_TOOL_FAILED：{text}"
        return text

    return StructuredTool.from_function(
        coroutine=_call,
        name=local_name,
        description=f"[来自 MCP 服务端 {client.name}] {description}",
        args_schema=args_model,
    )


def _result_text(result: dict[str, Any]) -> str:
    """把 MCP 的 content 块压成一段文本。

    只取 text 块：image / audio 是二进制，本地工具的输出契约是文本，
    混进 base64 会直接把上下文预算烧光。
    """
    blocks = result.get("content")
    if not isinstance(blocks, list):
        structured = result.get("structuredContent")
        return json.dumps(structured, ensure_ascii=False) if structured is not None else ""
    parts = [
        str(block.get("text", ""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)
