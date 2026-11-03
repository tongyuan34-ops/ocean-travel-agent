"""MCP（Model Context Protocol）接入层。

- ``protocol``：JSON-RPC 2.0 + 换行分帧 + 请求元数据（规范 2026-07-28）
- ``server``：把本项目的工具暴露给任何 MCP 客户端（Claude Desktop / Cursor）
- ``client``：连外部 MCP 服务端，把它们的工具变成 Agent 可调用的工具
- ``registry``：运行时注册表，让外部工具进入工具清单

两个方向都要，因为它们是同一个问题的两面：对外证明「我的工具能被别人用」，
对内证明「别人的工具我能编排」。只有其中一个，面试时讲不完整。
"""

from __future__ import annotations

from app.mcp.client import MCPClient, MCPError, StdioMCPClient
from app.mcp.server import MCPServer, serve_stdio

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPServer",
    "StdioMCPClient",
    "serve_stdio",
]
