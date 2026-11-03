"""MCP 线协议：JSON-RPC 2.0 + 换行分隔的 stdio 帧。

**为什么自己实现，而不是装官方 SDK**

MCP 的线协议本体很小：JSON-RPC 2.0、换行分帧、五六个方法。官方 Python SDK
会把 anyio / httpx / sse-starlette / pydantic 之外的一整套东西拉进来，而这个项目
依赖树是刻意收紧的（见 pyproject 的注释）。自己实现 200 行能省掉一整层传递依赖，
代价是版本兼容要自己盯着——所以下面把版本号和兼容规则都写成常量，而不是散落在判断里。

**规范在 2026-07-28 变了，这是最容易写错的地方**

2025-11-25 及更早：先 ``initialize`` 握手，服务端返回 ``protocolVersion``，
此后这个连接就带着会话状态。2026-07-28：**取消握手，改成无状态**——每个请求
自己在 ``params._meta`` 里带上协议版本、客户端信息、客户端能力：

    "params": {
      "name": "get_weather",
      "arguments": {...},
      "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "x", "version": "1.0"},
        "io.modelcontextprotocol/clientCapabilities": {}
      }
    }

服务端据此**逐请求**判断能不能服务，不再有「这个连接已经握手过了」这种概念。
同时新增 ``server/discover``（服务端 MUST 实现）用于报出自己支持的版本。

所以这里的实现是**双时代**（dual-era）：
- 收到带 ``_meta`` 的请求 → 按无状态语义处理
- 收到 ``initialize`` → 切到 legacy 语义，后续请求允许不带 ``_meta``

缺 ``protocolVersion`` 或 ``clientCapabilities`` 时规范要求返回 ``-32602``；
版本不认识时返回 ``-32022`` 并附上自己支持的版本列表——这条必须给，
否则老客户端没有任何诊断信息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# 当前实现的版本（2026-07-28 起为无状态协议）
PROTOCOL_VERSION = "2026-07-28"

# 兼容的老版本。握手式协议按日期递增，服务端接受哪个就按哪个的语义走；
# 实际行为差别只在「要不要 initialize」，工具调用本身没变
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

SUPPORTED_VERSIONS = (PROTOCOL_VERSION, *LEGACY_VERSIONS)

# 请求元数据键。前缀是反向域名，modelcontextprotocol 段由协议保留，
# 第三方不能自己造同前缀的键
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP 扩展错误码
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# 结果状态。目前只有 complete 和 input_required（需要用户补充输入再重试），
# 后者要配 MRTR 多轮往返，本服务端不支持，所以只产出 complete
RESULT_COMPLETE = "complete"


class ProtocolError(Exception):
    """协议层错误：以 JSON-RPC error 返回，而不是放进 result。"""

    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_message(self, request_id: Any) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


@dataclass(frozen=True)
class RequestMeta:
    """一个请求自带的元数据。现代版本里每个请求都有，legacy 里可能没有。"""

    protocol_version: str
    client_info: dict[str, Any] = field(default_factory=dict)
    client_capabilities: dict[str, Any] = field(default_factory=dict)

    @property
    def is_modern(self) -> bool:
        return True


def parse_meta(params: dict[str, Any] | None) -> RequestMeta | None:
    """从 ``params._meta`` 取元数据；没有 ``_meta`` 时返回 None（可能是 legacy）。

    只在**缺必填字段**时抛错——``clientInfo`` 是可选的（规范 SHOULD 带，不是 MUST），
    拿它当必填会把一些合法客户端挡在门外。
    """
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None

    version = meta.get(META_VERSION)
    if not isinstance(version, str) or not version.strip():
        raise ProtocolError(
            INVALID_PARAMS,
            "缺少必需的请求元数据",
            {"missing": [META_VERSION]},
        )

    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        # 规范把 clientCapabilities 定为必填。空对象是合法的「我不支持任何可选能力」
        raise ProtocolError(
            INVALID_PARAMS,
            "缺少必需的请求元数据",
            {"missing": [META_CLIENT_CAPABILITIES]},
        )

    info = meta.get(META_CLIENT_INFO)
    return RequestMeta(
        protocol_version=version,
        client_info=info if isinstance(info, dict) else {},
        client_capabilities=capabilities,
    )


def require_supported(version: str) -> str:
    """校验协议版本。不认识时必须报出自己支持哪些，否则客户端无从降级。"""
    if version in SUPPORTED_VERSIONS:
        return version
    raise ProtocolError(
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        {"supported": list(SUPPORTED_VERSIONS), "requested": version},
    )


def result_message(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_message(
    request_id: Any, code: int, message: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return ProtocolError(code, message, data).to_message(request_id)


def text_content(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def call_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """``tools/call`` 的成功/失败结果。

    工具**执行失败**走 ``isError: true`` 而不是 JSON-RPC error：
    前者是「模型该看到并自己纠正」，后者是「协议层出错，模型看不到内容」。
    把业务失败报成协议错误，模型就失去了自我修正的机会。
    """
    return {
        "resultType": RESULT_COMPLETE,
        "content": [text_content(text)],
        "isError": is_error,
    }


def encode(message: dict[str, Any]) -> str:
    """序列化成一行。

    ``ensure_ascii=False`` 是为了中文可读（日志和抓包时差别很大）；
    换行由 json.dumps 自动转义成 ``\\n``，不会破坏「一条消息一行」的分帧。
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def decode(line: str) -> dict[str, Any]:
    """解析一行 JSON-RPC。解析失败抛 ProtocolError（-32700）。"""
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(PARSE_ERROR, f"JSON 解析失败：{exc.msg}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(INVALID_REQUEST, "消息必须是 JSON 对象")
    if message.get("jsonrpc") != "2.0":
        raise ProtocolError(INVALID_REQUEST, "缺少 jsonrpc: 2.0")
    return message
