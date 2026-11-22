"""检索规模压测：构建耗时、内存、检索延迟随语料规模的变化。

输出一张 markdown 表，直接可以贴进 README 或面试材料。

用法::

    python scripts/bench_rag.py --corpus knowledge_probe
    python scripts/bench_rag.py --corpus knowledge_corpus --sizes 10000 50000 100000
"""

from __future__ import annotations

import argparse
import gc
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag import BM25Index, load_chunks  # noqa: E402

# 固定查询集：混合「旅游业务问题」和「百科事实问题」。
# 固定是为了让不同规模的数字可比——每次换查询，延迟就不可比了。
QUERIES = [
    "带两岁小孩的行程节奏怎么安排",
    "三天预算大概多少",
    "雨天有什么备选方案",
    "海岛轮渡停航怎么办",
    "哪些景点适合长辈",
    "昭通机场在哪里",
    "云南有哪些民用机场",
    "京杭大运河全长多少公里",
    "唐朝的都城是哪里",
    "茶叶的起源地",
    "什么是季风气候",
    "高速铁路最高运营速度",
    "敦煌莫高窟开凿于哪个朝代",
    "长江流经几个省",
    "中医的四大经典",
    "兵马俑在哪个城市",
    "世界上面积最大的淡水湖",
    "第一次世界大战的导火索",
    "光合作用的产物是什么",
    "珠穆朗玛峰的海拔",
]


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, round((len(ordered) - 1) * ratio))
    return ordered[position]


def _measure(
    chunks, queries, repeat: int, *, memory: bool = False
) -> tuple[dict[str, float], BM25Index]:
    """测一档规模。``memory=True`` 时用 tracemalloc 量峰值。

    两者不混着测：tracemalloc 会拦截每一次内存分配，实测把构建耗时放大
    4~5 倍（十万块 47s → 217s）。要报耗时就用干净的一遍。
    """
    gc.collect()
    if memory:
        tracemalloc.start()
    started = time.perf_counter()
    index = BM25Index(chunks)
    build_seconds = time.perf_counter() - started
    peak = 0.0
    if memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    latencies: list[float] = []
    hits = 0
    for _ in range(repeat):
        for query in queries:
            started = time.perf_counter()
            found = index.search(query, 4)
            latencies.append((time.perf_counter() - started) * 1000)
            hits += 1 if found else 0

    return {
        "chunks": float(index.size),
        "terms": float(index.term_count),
        "postings": float(index.posting_count),
        "build_s": build_seconds,
        "peak_mb": peak / 1024 / 1024,
        "p50_ms": statistics.median(latencies),
        "p95_ms": _percentile(latencies, 0.95),
        "hit_rate": hits / max(1, len(latencies)),
    }, index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索规模压测")
    parser.add_argument("--corpus", default="knowledge_corpus", help="语料目录")
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[10_000, 50_000, 100_000],
        help="要测的块数档位",
    )
    parser.add_argument("--repeat", type=int, default=3, help="每个查询重复几次")
    parser.add_argument(
        "--memory",
        action="store_true",
        help="用 tracemalloc 量峰值内存（会显著拉长构建耗时，单独跑）",
    )
    parser.add_argument("--cache-bench", action="store_true", help="额外测落盘与加载耗时")
    args = parser.parse_args(argv)

    corpus = PROJECT_ROOT / args.corpus
    started = time.perf_counter()
    chunks_all = load_chunks(corpus)
    load_seconds = time.perf_counter() - started
    print(f"语料 {corpus}：{len(chunks_all)} 个块，切块 {load_seconds:.1f}s\n")
    if not chunks_all:
        print("语料为空，先跑 scripts/build_corpus.py")
        return 1

    rows: list[dict[str, float]] = []
    last_index: BM25Index | None = None
    for size in args.sizes:
        if size > len(chunks_all):
            print(f"跳过 {size}：语料只有 {len(chunks_all)} 个块")
            continue
        chunks = chunks_all[:size]
        row, last_index = _measure(chunks, QUERIES, args.repeat, memory=args.memory)
        rows.append(row)
        memory_text = f"{row['peak_mb']:.1f}MB" if args.memory else "—"
        print(
            f"{size:>7} 块  构建 {row['build_s']:6.1f}s  峰值 {memory_text:>8}  "
            f"P50 {row['p50_ms']:5.1f}ms  P95 {row['p95_ms']:5.1f}ms  "
            f"命中率 {row['hit_rate']:.0%}"
        )
        del chunks
        gc.collect()

    print("\n| 块数 | term 数 | 倒排条目 | 构建耗时 | 峰值内存 | P50 | P95 | 查询命中率 |")
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        memory_text = f"{row['peak_mb']:.1f}MB" if args.memory else "—"
        print(
            f"| {int(row['chunks'])} | {int(row['terms'])} | {int(row['postings'])} "
            f"| {row['build_s']:.1f}s | {memory_text} "
            f"| {row['p50_ms']:.1f}ms | {row['p95_ms']:.1f}ms | {row['hit_rate']:.0%} |"
        )

    if args.cache_bench and last_index is not None:
        target = PROJECT_ROOT / "data" / "index" / "bench.pkl"
        started = time.perf_counter()
        written = last_index.save(target)
        save_seconds = time.perf_counter() - started
        started = time.perf_counter()
        restored = BM25Index.load(target)
        load_back = time.perf_counter() - started
        # 恢复后必须给出完全一样的结果，否则缓存就是在偷偷改变行为
        same = all(
            [h.chunk.citation for h in last_index.search(q, 4)]
            == [h.chunk.citation for h in restored.search(q, 4)]
            for q in QUERIES
        )
        print(
            f"\n落盘 {written / 1024 / 1024:.1f}MB 用时 {save_seconds:.1f}s，"
            f"加载 {load_back:.1f}s，恢复后 {restored.size} 个块，结果一致：{same}"
        )
        target.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
