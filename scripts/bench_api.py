"""HTTP 压测与单轮对话采样，产出写进 README 的真实数字。

用法（服务先起在 8000）：

    # 只读端点压测，不花模型额度
    python scripts/bench_api.py --base-url http://127.0.0.1:8000 --duration 10 --concurrency 20

    # 顺带跑 3 轮真实对话，量首字延迟、总耗时、token 与成本
    python scripts/bench_api.py --chat 3

为什么不用 locust/k6：两者都要另装、另学一套配置，而 README 里需要的
只有「并发、吞吐、P50/P95/P99」四个数。60 行 asyncio 足够了。

**只读压测和对话压测必须分开**：对话每次都烧模型额度，把它混进吞吐测试里，
数字会同时受上游限流影响，既测不准服务端，也测不准模型。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PATHS = ("/api/v1/health", "/api/v1/skills", "/api/v1/tools")


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[index]


def _summary(latencies: list[float], errors: dict[str, int], elapsed: float) -> dict[str, Any]:
    ordered = sorted(latencies)
    total = len(ordered) + sum(errors.values())
    return {
        "requests": total,
        "errors": sum(errors.values()),
        "error_kinds": errors,
        "throughput_rps": round(len(ordered) / elapsed, 2) if elapsed > 0 else 0.0,
        "p50_ms": round(_percentile(ordered, 0.50), 1),
        "p95_ms": round(_percentile(ordered, 0.95), 1),
        "p99_ms": round(_percentile(ordered, 0.99), 1),
        "max_ms": round(ordered[-1], 1) if ordered else 0.0,
        "mean_ms": round(statistics.fmean(ordered), 1) if ordered else 0.0,
    }


async def _load(args: argparse.Namespace, headers: dict[str, str]) -> dict[str, Any]:
    paths = [item.strip() for item in args.paths.split(",") if item.strip()]
    latencies: list[float] = []
    errors: dict[str, int] = {}
    deadline = time.perf_counter() + args.duration
    started = time.perf_counter()

    limits = httpx.Limits(max_connections=args.concurrency * 2)
    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=args.timeout, limits=limits
    ) as client:

        async def worker(offset: int) -> None:
            index = offset
            while time.perf_counter() < deadline:
                path = paths[index % len(paths)]
                index += 1
                begin = time.perf_counter()
                try:
                    response = await client.get(path)
                except httpx.HTTPError as exc:
                    kind = type(exc).__name__
                    errors[kind] = errors.get(kind, 0) + 1
                    continue
                latencies.append((time.perf_counter() - begin) * 1000)
                if response.status_code >= 400:
                    kind = f"HTTP {response.status_code}"
                    errors[kind] = errors.get(kind, 0) + 1

        await asyncio.gather(*(worker(i) for i in range(args.concurrency)))

    elapsed = time.perf_counter() - started
    return {**_summary(latencies, errors, elapsed), "elapsed_seconds": round(elapsed, 2)}


async def _chat_turn(
    client: httpx.AsyncClient, session_id: str, message: str, timeout: float
) -> dict[str, Any]:
    """发一条消息，读完 SSE，返回首字/总耗时与用量。"""
    payload = {"message": message, "attachments": []}
    begin = time.perf_counter()
    first_token_ms: float | None = None
    usage: dict[str, Any] = {}
    error = ""
    tokens = 0

    async with client.stream(
        "POST", f"/api/v1/chat/sessions/{session_id}/messages", json=payload, timeout=timeout
    ) as response:
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", "replace")
            return {"error": f"HTTP {response.status_code}: {body[:200]}"}
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "token":
                tokens += 1
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - begin) * 1000
            elif kind == "error":
                error = str(event.get("message") or event.get("code") or "error")
            elif kind == "end":
                usage = (event.get("usage") or {}).get("turn") or {}
                break

    total_ms = (time.perf_counter() - begin) * 1000
    return {
        "first_token_ms": round(first_token_ms, 1) if first_token_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "token_events": tokens,
        "usage": usage,
        "error": error,
    }


async def _chat(args: argparse.Namespace, headers: dict[str, str]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=args.timeout
    ) as client:
        created = await client.post("/api/v1/chat/sessions")
        created.raise_for_status()
        session_id = created.json()["session_id"]
        print(f"对话压测会话：{session_id}", file=sys.stderr)

        results = []
        for index, prompt in enumerate(args.prompts, start=1):
            result = await _chat_turn(client, session_id, prompt, args.chat_timeout)
            result["prompt"] = prompt
            results.append(result)
            mark = "ERR" if result.get("error") else "OK "
            print(
                f"[{index}/{len(args.prompts)}] {mark} 首字={result.get('first_token_ms')}ms "
                f"总={result.get('total_ms')}ms tokens={result.get('usage', {}).get('total_tokens')} "
                f"cost={result.get('usage', {}).get('cost')}",
                file=sys.stderr,
            )
        return results


def _render(load: dict[str, Any], chats: list[dict[str, Any]], args: argparse.Namespace) -> str:
    lines = [
        "",
        f"只读压测 | {args.base_url} | 并发 {args.concurrency} | 持续 {load['elapsed_seconds']}s",
        f"请求 {load['requests']}  错误 {load['errors']}  吞吐 {load['throughput_rps']} req/s",
        f"P50 {load['p50_ms']}ms  P95 {load['p95_ms']}ms  P99 {load['p99_ms']}ms  max {load['max_ms']}ms",
    ]
    ok = [item for item in chats if not item.get("error")]
    if chats:
        lines.append("")
        lines.append("真实对话")
        for item in chats:
            if item.get("error"):
                lines.append(f"- 失败：{item['error']}")
                continue
            total = item["usage"].get("total_tokens")
            cost = item["usage"].get("cost")
            lines.append(
                f"- 首字 {item['first_token_ms']}ms  总 {item['total_ms']}ms  "
                f"tokens {total}  成本 {cost} 元"
            )
        if ok:
            lines.append(
                f"- 合计 tokens {sum(int(i['usage'].get('total_tokens') or 0) for i in ok)}  "
                f"成本 {round(sum(float(i['usage'].get('cost') or 0) for i in ok), 6)} 元"
            )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench_api", description="HTTP 压测与对话采样")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=10.0, help="压测持续秒数")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--paths", default=",".join(DEFAULT_PATHS), help="逗号分隔的只读端点")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--token", default="", help="TENANT_TOKENS 模式下的 Bearer 令牌")
    parser.add_argument("--chat", type=int, default=0, help="额外跑几条真实对话（会花模型额度）")
    parser.add_argument("--chat-timeout", type=float, default=180.0)
    parser.add_argument(
        "--prompt",
        action="append",
        dest="prompts",
        help="对话内容，可重复；不传则用内置的三条",
    )
    parser.add_argument("--json", default="", help="把结果写成 JSON 的路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    prompts = args.prompts or [
        "用一句话介绍杭州最适合散步的地方",
        "北京今天天气怎么样？",
        "帮我算一下两个人去成都玩四天的预算",
    ]
    args.prompts = prompts[: args.chat] if args.chat else []

    async def run() -> int:
        load = await _load(args, headers)
        chats = await _chat(args, headers) if args.chat else []
        print(_render(load, chats, args))
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(
                json.dumps(
                    {
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "base_url": args.base_url,
                        "concurrency": args.concurrency,
                        "duration_seconds": args.duration,
                        "load": load,
                        "chat": chats,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
