"""测试用的假 MCP 服务端：可控地扮演「新版」和「老版」两种。

为什么要自己写一个，而不是拿 ``app.mcp.server`` 当对端：本项目的服务端是双时代
的，连它永远走 modern 分支，``initialize`` 回退那条路就测不到。而这条路径恰恰
最容易写错——判断错了就是「连接成功但所有调用都失败」。

用法：``python tests/fake_mcp_server.py --mode modern|legacy``
"""

from __future__ import annotations

import argparse
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "原样返回 text",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的内容"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "永远返回 isError",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "boom",
        "description": "抛异常，用于验证协议层兜底",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "noisy",
        "description": "往 stderr 大量输出，验证客户端会排空管道",
        "inputSchema": {"type": "object", "properties": {"lines": {"type": "integer"}}},
    },
]


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _call(params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if name == "echo":
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": arguments.get("text", "")}],
        }
    if name == "fail":
        return {
            "resultType": "complete",
            "content": [{"type": "text", "text": "INVALID_REQUEST：参数不对"}],
            "isError": True,
        }
    if name == "boom":
        raise RuntimeError("故意炸的")
    if name == "noisy":
        # 故意刷满 stderr：客户端不排空的话，这里会阻塞在 write 上
        for _ in range(int(arguments.get("lines", 2000))):
            sys.stderr.write("x" * 200 + "\n")
        sys.stderr.flush()
        return {"resultType": "complete", "content": [{"type": "text", "text": "ok"}]}
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["modern", "legacy", "silent"], default="modern")
    args = parser.parse_args()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in message:
            continue  # 通知不回
        if args.mode == "silent":
            # 读了但永不回复：用来验证客户端的超时与取消路径
            continue

        method = message.get("method")
        params = message.get("params") or {}
        request_id = message["id"]

        if method == "server/discover":
            if args.mode == "legacy":
                # 老版服务端不认识这个方法：实现自定的错误码，规范说客户端不能只认某个码
                response = _error(request_id, -32601, "Method not found")
            else:
                response = _result(
                    request_id,
                    {
                        "resultType": "complete",
                        "supportedVersions": ["2026-07-28"],
                        "capabilities": {"tools": {}},
                    },
                )
        elif method == "initialize":
            version = params.get("protocolVersion", "2025-06-18")
            response = _result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0.0.1"},
                },
            )
        elif method == "tools/list":
            response = _result(request_id, {"resultType": "complete", "tools": TOOLS})
        elif method == "tools/call":
            payload = _call(params)
            if payload is None:
                response = _error(request_id, -32602, f"Unknown tool: {params.get('name')}")
            else:
                response = _result(request_id, payload)
        elif method == "ping":
            response = _result(request_id, {})
        else:
            response = _error(request_id, -32601, "Method not found")

        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
