"""离线批量调用模型（火山方舟 service_tier=flex 低优推理）。

为什么单独做脚本而不是改在线聊天：
在线回答需要逐 token 推送、工具调用与权限确认，这些是流式交互；
批量推理/低优推理面向**时延不敏感**的后台任务，不保证首字延迟与成功率，
因此只用于离线跑一批提示词，不接入 /api/v1/chat 链路。

用法：
    .venv\\Scripts\\python.exe scripts/batch_chat.py 输入.jsonl 输出.jsonl --concurrency 4

输入：每行一个 JSON，形如 {"id": "q1", "prompt": "帮我规划大理三日游"}
输出：每行一个 JSON，含 id、prompt、answer、usage 或 error，逐行落盘，中断可续跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_openai import ChatOpenAI  # noqa: E402

from app.core.config import load_settings  # noqa: E402


def build_model(tier: str) -> ChatOpenAI:
    """构造与项目一致的模型客户端，按需附加 service_tier。"""
    settings = load_settings()
    if not settings.model_name or not settings.model_base_url:
        raise SystemExit("MODEL_NAME / MODEL_BASE_URL 未配置，请先填 .env")

    host = urlparse(settings.model_base_url).hostname or ""
    extra: dict[str, Any] = {}
    if host == "api.deepseek.com" or host.endswith("volces.com"):
        extra["thinking"] = {"type": "disabled"}
    if tier and tier != "auto":
        # 火山方舟：flex = 在线推理（低优），价格更低、配额独立，但不承诺时延
        extra["service_tier"] = tier

    return ChatOpenAI(
        model=settings.model_name,
        api_key=settings.model_api_key or "not-needed",
        base_url=settings.model_base_url,
        use_responses_api=False,
        timeout=300,
        max_tokens=4096,
        max_retries=0,
        extra_body=extra or None,
    )


def read_prompts(path: Path) -> list[dict[str, str]]:
    """读取 JSONL 输入；空行跳过，id 缺省时用行号补齐。"""
    items: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError as exc:
                raise SystemExit(f"第 {line_no} 行不是合法 JSON：{exc}") from exc
            prompt = str(item.get("prompt") or "").strip()
            if not prompt:
                raise SystemExit(f"第 {line_no} 行缺少 prompt")
            items.append({"id": str(item.get("id") or line_no), "prompt": prompt})
    return items


async def run(
    model: ChatOpenAI,
    items: list[dict[str, str]],
    output: Path,
    concurrency: int,
) -> tuple[int, int]:
    """并发跑完全部提示词，逐行追加结果。"""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    write_lock = asyncio.Lock()
    ok = failed = 0

    async def one(item: dict[str, str]) -> None:
        nonlocal ok, failed
        async with semaphore:
            try:
                reply = await model.ainvoke(item["prompt"])
                record: dict[str, Any] = {
                    "id": item["id"],
                    "prompt": item["prompt"],
                    "answer": reply.content,
                    "usage": getattr(reply, "usage_metadata", None),
                }
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                record = {"id": item["id"], "prompt": item["prompt"], "error": str(exc)}
                failed += 1
            async with write_lock:
                with output.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    await asyncio.gather(*(one(item) for item in items))
    return ok, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="离线批量调用模型")
    parser.add_argument("input", type=Path, help="输入 JSONL，每行含 prompt")
    parser.add_argument("output", type=Path, help="输出 JSONL，逐行追加")
    parser.add_argument("--concurrency", type=int, default=4, help="并发数，默认 4")
    parser.add_argument(
        "--tier",
        default="auto",
        choices=["auto", "flex", "fast"],
        help=(
            "火山方舟服务档位：auto 常规；flex 低优更便宜，"
            "但需模型版本支持（当前 deepseek-v4-1-flash-economy 会返回 400）"
        ),
    )
    args = parser.parse_args()

    items = read_prompts(args.input)
    if not items:
        raise SystemExit("输入为空")

    model = build_model(args.tier)
    ok, failed = asyncio.run(run(model, items, args.output, args.concurrency))
    print(f"完成：成功 {ok} 条，失败 {failed} 条，结果写入 {args.output}")


if __name__ == "__main__":
    main()
