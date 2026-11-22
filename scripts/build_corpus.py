"""把公开中文语料规范化成知识库格式，用于大规模检索压测。

**为什么单独放一个目录**

``knowledge/`` 是产品知识库（5 篇人工维护的旅行规则，会进模型的提示词），
这个脚本产出的是**压测语料**，语料量级差四个数量级，混在一起会让
``search_knowledge`` 检索出一堆维基百科条目。所以默认写到
``knowledge_corpus/``，只在压测时显式指过去。

**数据来源**

``0xDing/wikipedia-cn-20230720-filtered``（中文维基百科过滤版，CC BY-SA）。
原始文件 524 MB，但压测不需要全量：脚本边下边解析，凑够目标块数就停，
所以实际只下载几十到一百多 MB。

**为什么要流式解析**

把 500 MB JSON 读成 Python 对象再遍历，峰值内存约 1.5 GB，还没开始压测
就把机器压垮了。这里用增量解码 + ``raw_decode`` 逐个对象吐出来，缓冲区
只保留「当前还没解析完的那几 KB」，内存跟文件大小无关。

用法::

    python scripts/build_corpus.py --target-chunks 100000
    python scripts/build_corpus.py --target-chunks 10000 --out knowledge_small
"""

from __future__ import annotations

import argparse
import codecs
import json
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SOURCE_URL = (
    "https://hf-mirror.com/datasets/0xDing/wikipedia-cn-20230720-filtered"
    "/resolve/main/wikipedia-cn-20230720-filtered.json"
)
_READ_BLOCK = 1 << 20  # 1 MB
_MIN_TEXT_CHARS = 200  # 太短的条目切不出有意义的块


def _iter_records(response, byte_budget: int):
    """从 HTTP 响应流里逐个吐出 JSON 对象，内存占用与文件大小无关。"""
    decoder = json.JSONDecoder()
    incremental = codecs.getincrementaldecoder("utf-8")("ignore")
    buffer = ""
    cursor = 0
    budget = byte_budget

    while True:
        # 先把缓冲区里能解析的对象全部吐出来
        while True:
            while cursor < len(buffer) and buffer[cursor] in " \t\r\n,[":
                cursor += 1
            if cursor >= len(buffer):
                break
            try:
                obj, end = decoder.raw_decode(buffer, cursor)
            except json.JSONDecodeError:
                break  # 对象被块边界截断，等下一次读取补全
            cursor = end
            yield obj

        if budget <= 0:
            return
        raw = response.read(_READ_BLOCK)
        if not raw:
            return
        budget -= len(raw)
        # 丢掉已消费的前缀，避免缓冲区随文件增长
        buffer = buffer[cursor:] + incremental.decode(raw)
        cursor = 0


def _iter_source(budget_bytes: int):
    request = urllib.request.Request(
        SOURCE_URL,
        headers={
            # 拿一段就够：Range 让服务端别把 524 MB 全推过来
            "Range": f"bytes=0-{budget_bytes - 1}",
            "User-Agent": "OceanTravelAgent-corpus-builder/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        yield from _iter_records(response, budget_bytes)


def _clean(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # 条目正文里自带的 # 会把 split_markdown 误判成小标题
        lines.append(stripped.lstrip("#").strip())
    return "\n\n".join(lines)


def build(out_dir: Path, target_chunks: int, per_file: int, budget_bytes: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("wiki-*.md"):
        stale.unlink()

    started = time.perf_counter()
    articles = 0
    written = 0
    pending: list[str] = []
    skipped = 0

    for record in _iter_source(budget_bytes):
        # 这个数据集的字段是 completion（正文）+ source（来源标识），
        # 没有独立标题——用第一个句子当标题，供 BM25 的字段加权使用。
        text = record.get("text") or record.get("content") or record.get("completion") or ""
        title = str(record.get("title") or "").strip()
        if not title:
            title = text.split("。", 1)[0].strip()[:40]
        if not title or len(text) < _MIN_TEXT_CHARS:
            skipped += 1
            continue
        body = _clean(text)
        if len(body) < _MIN_TEXT_CHARS:
            skipped += 1
            continue
        pending.append(f"# {title}\n\n{body}\n")
        articles += 1

        if len(pending) >= per_file:
            written += 1
            path = out_dir / f"wiki-{written:04d}.md"
            path.write_text("\n".join(pending), encoding="utf-8")
            pending = []
            # 每写完一批就估一次块数，够量就停——不用等全部下载完
            if articles % (per_file * 4) == 0 and _rough_chunks(articles) >= target_chunks:
                break

    if pending:
        written += 1
        (out_dir / f"wiki-{written:04d}.md").write_text("\n".join(pending), encoding="utf-8")

    elapsed = time.perf_counter() - started
    print(f"抓取条目 {articles}，跳过 {skipped}，写出 {written} 个文件，耗时 {elapsed:.1f}s")


def _rough_chunks(articles: int) -> int:
    """按经验值估算块数：一篇中文维基条目约 2.5 个块（正文 1~3 KB）。"""
    return int(articles * 2.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建大规模中文语料库")
    parser.add_argument("--out", default="knowledge_corpus", help="输出目录")
    parser.add_argument("--target-chunks", type=int, default=100_000, help="目标块数")
    parser.add_argument("--per-file", type=int, default=200, help="每个 md 文件收录多少条目")
    parser.add_argument(
        "--max-mb",
        type=int,
        default=400,
        help="最多下载多少 MB 原始数据（凑够块数会提前停止）",
    )
    args = parser.parse_args(argv)

    out_dir = PROJECT_ROOT / args.out
    budget = args.max_mb * 1024 * 1024
    build(out_dir, args.target_chunks, args.per_file, budget)

    from app.rag import load_knowledge_base  # 延迟导入：--help 不该付索引的初始化成本

    kb = load_knowledge_base(out_dir)
    files = len(list(out_dir.glob("*.md")))
    print(
        f"目录 {out_dir}：{files} 个文件，{kb.size} 个块，"
        f"{kb.index.term_count} 个 term，{kb.index.posting_count} 条倒排"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
