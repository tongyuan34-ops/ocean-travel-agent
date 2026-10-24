"""本地前端服务器：静态页面 + /api 同源反向代理（仅标准库）。

为什么需要它：
1. 前端约定跑在 3001 端口，后端在 8000 端口；
2. 通过本脚本访问时，页面和接口同源，手机用局域网 IP 打开也不会被 CORS 拦住；
3. SSE 回复按块转发，不缓冲，聊天可以逐字出现。

用法：
    python web/serve.py            # 默认 0.0.0.0:3001，转发到 127.0.0.1:8000
    python web/serve.py --port 3001 --backend http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import mimetypes
import os
import socket
import sys
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 让 .webmanifest 以正确 MIME 返回，手机端「添加到主屏幕」才会识别
mimetypes.add_type("application/manifest+json", ".webmanifest")
HOP_HEADERS = {"connection", "transfer-encoding", "content-length", "keep-alive"}
# 显式禁用环境代理：代理只连本机后端，走系统代理会破坏 SSE 的实时性
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class FrontendHandler(SimpleHTTPRequestHandler):
    """静态文件 + /api 代理。"""

    backend = "http://127.0.0.1:8000"
    # (user, password)；留空表示不启用 Basic Auth
    auth: tuple[str, str] = ("", "")
    protocol_version = "HTTP/1.0"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[web] %s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # ---------- 访问密码 ----------

    def _is_local_preview(self) -> bool:
        """本机 localhost 预览免密码。

        IDE 内置浏览器/预览面板通常弹不出 Basic Auth 对话框，会直接显示 401。
        这里只放行「回环地址 + Host 为 localhost/127.0.0.1 + 没有经过 Cloudflare」的请求：
        - 局域网访问的 Host 是 192.168.x.x，仍需密码；
        - 公网隧道会带 CF-Connecting-IP / X-Forwarded-For，仍需密码；
        避免本地预览方便的同时把公网入口一起放空。
        """
        if self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For"):
            return False
        raw_host = (self.headers.get("Host") or "").split(",")[0].strip()
        host = (
            raw_host.rsplit(":", 1)[0].strip("[]").lower() if ":" in raw_host else raw_host.lower()
        )
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return False
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def _authorized(self) -> bool:
        user, password = self.auth
        if not user or not password:
            return True
        if self._is_local_preview():
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        got_user, _, got_password = decoded.partition(":")
        return hmac.compare_digest(got_user, user) and hmac.compare_digest(got_password, password)

    def _unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Ocean", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        # 只读分享页要对拿到链接的人开放，不要求登录；其余路径仍走密码
        if self.path.startswith("/share/"):
            self._proxy("GET")
            return
        if not self._authorized():
            self._unauthorized()
            return
        if self.path.startswith("/api/"):
            self._proxy("GET")
        else:
            super().do_GET()

    def do_HEAD(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return
        if self.path.startswith("/api/"):
            self._proxy("POST")
        else:
            self.send_error(404, "not found")

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._unauthorized()
            return
        if self.path.startswith("/api/"):
            self._proxy("DELETE")
        else:
            self.send_error(404, "not found")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.end_headers()

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        request = urllib.request.Request(self.backend + self.path, data=body, method=method)
        for header in ("Content-Type", "Accept", "X-Device-ID", "Last-Event-ID"):
            value = self.headers.get(header)
            if value:
                request.add_header(header, value)

        try:
            response = OPENER.open(request, timeout=600)
        except urllib.error.HTTPError as exc:
            self._relay_error(exc)
            return
        except Exception as exc:  # noqa: BLE001 - 代理失败要如实返回给前端
            payload = json.dumps(
                {"code": "PROXY_ERROR", "message": "无法连接后端：%s" % exc},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        with response:
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() in HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            try:
                # 必须按行转发：read(n) 会等满 n 字节才返回，
                # 权限请求这类小事件会被压住，直到 120 秒超时后才到前端
                while True:
                    line = response.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # 前端主动停止生成时会断开连接，属于正常情况
                return

    def _relay_error(self, exc: urllib.error.HTTPError) -> None:
        data = exc.read()
        self.send_response(exc.code)
        content_type = exc.headers.get("Content-Type") if exc.headers else "application/json"
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def read_auth() -> tuple[str, str]:
    """从 .env / 进程环境读取 Basic Auth 账号；缺省表示不启用。"""
    values: dict[str, str] = {}
    env_path = ROOT.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    user = os.getenv("WEB_AUTH_USER") or values.get("WEB_AUTH_USER", "")
    password = os.getenv("WEB_AUTH_PASS") or values.get("WEB_AUTH_PASS", "")
    return (user, password)


def lan_ip() -> str:
    """取本机局域网地址，方便手机访问。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ocean 前端本地服务器")
    parser.add_argument("--port", type=int, default=3001, help="监听端口，默认 3001")
    parser.add_argument(
        "--backend", default="http://127.0.0.1:8000", help="后端地址，默认 http://127.0.0.1:8000"
    )
    parser.add_argument("--no-auth", action="store_true", help="不启用访问密码（仅限本机调试）")
    args = parser.parse_args()

    FrontendHandler.backend = args.backend.rstrip("/")
    auth = ("", "") if args.no_auth else read_auth()
    FrontendHandler.auth = auth
    server = ThreadingHTTPServer(("0.0.0.0", args.port), FrontendHandler)
    server.daemon_threads = True

    print("前端已启动：http://localhost:%d" % args.port)
    print("手机同网访问：http://%s:%d" % (lan_ip(), args.port))
    print("接口代理：/api/v1/**  ->  %s" % FrontendHandler.backend)
    print("访问密码：%s" % ("已启用（用户 %s）" % auth[0] if auth[0] else "未启用"))
    print("后端需另行启动：.venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
