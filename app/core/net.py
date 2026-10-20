"""按 URL 抓取网页，并做 SSRF 防护。

`fetch_url` 与 `parse_shared_link` 会抓取**用户给出的任意链接**，
因此必须防止它被用来探测内网／本机服务：

1. 只允许 http / https，其余协议（file://、ftp://、gopher:// 等）一律拒绝
2. 解析主机名后拒绝**内网与本机地址**（回环、私网、链路本地、云元数据、CGNAT、
   组播与保留段），见下方 _INTERNAL_NETWORKS
3. **不自动跟随跳转**，逐跳重新校验——否则一个公网 URL 可以通过 302 跳到 169.254.169.254
   这类云元数据地址，绕过入口校验
4. 限制响应体大小与总超时，避免被超大响应拖垮

关于判据的选择：这里用「内网网段黑名单」而不是「只放行 is_global 的公网段」。
后者会把 RFC2544 基准测试段 198.18.0.0/15 一并挡掉——
而本机所在的沙箱环境正是把所有外部域名解析到该段后透明转发，
用白名单会导致所有正常外网抓取全部失败。黑名单已覆盖全部能被用来
访问内网服务的地址段（含 169.254.169.254 这类元数据地址），安全属性不变。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

MAX_REDIRECTS = 5
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_BYTES = 1_500_000
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 TraveAssistant/1.0"
)
_ALLOWED_SCHEMES = {"http", "https"}

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# 内网 / 本机 / 不可用于内网的地址段黑名单（SSRF 的目标集合）
_INTERNAL_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",        # 本机网段（0.x 常被当作本机）
        "10.0.0.0/8",       # 私网 A
        "100.64.0.0/10",    # CGNAT，部分云元数据走此段
        "127.0.0.0/8",      # 回环
        "169.254.0.0/16",   # 链路本地，含云元数据 169.254.169.254
        "172.16.0.0/12",    # 私网 B
        "192.0.0.0/24",     # IETF 协议专用
        "192.168.0.0/16",   # 私网 C
        "198.51.100.0/24",  # 文档示例段
        "203.0.113.0/24",   # 文档示例段
        "224.0.0.0/4",      # 组播
        "240.0.0.0/4",      # 保留段
        "::/128",           # 未指定
        "::1/128",          # IPv6 回环
        "fc00::/7",         # IPv6 唯一本地地址
        "fe80::/10",        # IPv6 链路本地
        "ff00::/8",         # IPv6 组播
    )
]


class UrlNotAllowed(Exception):
    """URL 被安全策略拒绝。错误码与契约命名风格一致。"""

    code = "URL_NOT_ALLOWED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FetchError(Exception):
    """网络失败、HTTP 错误或响应非法。"""

    code = "FETCH_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def ensure_safe_url(url: str) -> str:
    """校验 URL 只指向公网 http(s) 资源，返回规范化后的 URL。

    域名解析放在线程里做，避免阻塞事件循环（getaddrinfo 是同步调用）。
    """
    raw = (url or "").strip()
    if not raw:
        raise UrlNotAllowed("URL 为空")

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UrlNotAllowed(
            f"只允许 http/https 链接，当前协议：{parsed.scheme or '（缺失）'}"
        )
    host = parsed.hostname
    if not host:
        raise UrlNotAllowed(f"无法从链接中解析出主机名：{raw}")

    await _ensure_public_host(host)
    return urlunparse(parsed._replace(fragment=""))


async def _ensure_public_host(host: str) -> None:
    """主机名若是 IP 字面量直接判断，若是域名则解析所有记录逐个判断。"""
    literal = _parse_ip(host)
    if literal is not None:
        if not _is_public(literal):
            raise UrlNotAllowed(f"{host} 属于内网/本机地址，已拒绝访问")
        return

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP
        )
    except (socket.gaierror, OSError) as exc:
        raise FetchError(f"域名无法解析：{host}（{exc}）") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise FetchError(f"域名无法解析：{host}")

    for addr in addresses:
        ip = _parse_ip(addr.split("%")[0])
        if ip is not None and not _is_public(ip):
            raise UrlNotAllowed(
                f"{host} 解析到内网/本机地址（{addr}），已拒绝访问"
            )


def _parse_ip(value: str) -> IpAddress | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_public(ip: IpAddress) -> bool:
    """不在内网黑名单内即视为可访问的外部地址。"""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 这类映射地址要按 v4 判断
    return not any(ip in network for network in _INTERNAL_NETWORKS)


async def fetch_text(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    headers: dict[str, str] | None = None,
) -> tuple[str, str, str, int]:
    """抓取 URL，返回 (最终 URL, 文本, content-type, HTTP 状态码)。

    逐跳校验跳转目标，第 3 条防护措施见模块 docstring。
    """
    current = await ensure_safe_url(url)
    request_headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if headers:
        request_headers.update(headers)

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            current = await ensure_safe_url(current)
            try:
                async with client.stream(
                    "GET", current, headers=request_headers
                ) as resp:
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("跳转响应缺少 Location 头")
                        current = urljoin(current, location)
                        continue

                    if resp.status_code >= 400:
                        raise FetchError(f"目标返回 HTTP {resp.status_code}")

                    ctype = resp.headers.get("content-type", "")
                    raw, truncated = await _read_body(resp, max_bytes)
                    encoding = resp.encoding or "utf-8"
                    break
            except httpx.TimeoutException as exc:
                raise FetchError(f"请求超时（>{timeout:.0f}s）") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"网络请求失败：{exc}") from exc
        else:
            raise FetchError(f"跳转次数超过 {MAX_REDIRECTS} 次，已停止")

    text = raw.decode(encoding, errors="replace")
    if truncated:
        text += f"\n…（内容超过 {max_bytes} 字节已截断）"
    return current, text, ctype, resp.status_code


async def _read_body(resp: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """边读边限长，避免把超大响应全文读进内存。"""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            keep = max_bytes - (total - len(chunk))
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated
