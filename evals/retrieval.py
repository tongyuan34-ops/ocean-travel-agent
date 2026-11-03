"""检索回归评测：知识库查得准不准，用带标注的用例集来守。

**和 ``evals/run.py`` 的区别**

``run.py`` 评测的是**模型**：要花 API 钱、要 LLM-as-judge、结果是概率的。
这个脚本评的是**检索器**：纯本地、纯确定、一分不花——同一组参数跑出来
必然是同一个分数，所以它适合进 CI（``tests/test_rag_recall.py`` 就是用它定的基线）。

**跑法**

    python -m evals.retrieval                # 出报告
    python -m evals.retrieval --fail-under 0.8   # 低于阈值就非零退出
    python -m evals.retrieval --sweep        # RELATIVE_FLOOR × FIELD_BOOST 全网格
    python -m evals.retrieval --limit 5      # 只看前几条

**指标**

- ``recall@1`` / ``recall@3``：期望小节有没有进前 1 / 前 3。工具默认返回 3 条，
  所以 @3 是主要指标，@1 看「第一名是不是对的」。
- ``MRR``：第一个期望命中的位置倒数，看「排得靠前还是靠后」。
- ``negative@空``：负例里返回空的比例。相对阈值（``RELATIVE_FLOOR``）防的就是
  这个——它调低了正例全过、负例全灭，比 recall 更能暴露阈值调过头。

``known_fail`` 的用例**计入报告但不计入基线**：它是已知未命中——要么查询本身
有歧义，要么是检索器的能力边界（比如单字概念对不上二元组）。把它算进基线
会让基线变成「永远过不了 100%」的摆设；但留在报告里，哪天它过了说明改对了。

``tests/test_rag_recall.py`` 把这条约束钉死：**实际失败集合必须恰好等于**
yaml 里声明的 ``known_fail``。多一条是回归，少一条是修好了忘了删标记，
两种情况都会红。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.rag.chunk import Chunk, split_markdown
from app.rag.index import FIELD_BOOST, RELATIVE_FLOOR, BM25Index

CASES_PATH = Path(__file__).resolve().parent / "retrieval_cases.yaml"
KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"


@dataclass(frozen=True)
class Case:
    id: str
    query: str
    expect: tuple[str, ...]
    known_fail: bool

    @property
    def is_negative(self) -> bool:
        return not self.expect


@dataclass(frozen=True)
class Outcome:
    case: Case
    hits: tuple[str, ...]

    @property
    def first_hit_rank(self) -> int | None:
        for index, citation in enumerate(self.hits):
            if citation in self.case.expect:
                return index
        return None

    @property
    def hit_at_1(self) -> bool:
        return self.first_hit_rank == 0

    @property
    def hit_at_3(self) -> bool:
        rank = self.first_hit_rank
        return rank is not None and rank < 3

    @property
    def negative_ok(self) -> bool:
        return not self.hits


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    """读用例集。id 重名直接报错——回归集里的重复 id 意味着后一条永远跑不到。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{path} 里没有 cases 列表")

    seen: set[str] = set()
    cases: list[Case] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("query"):
            raise ValueError(f"用例缺 id 或 query：{item!r}")
        case_id = str(item["id"])
        if case_id in seen:
            raise ValueError(f"用例 id 重复：{case_id}")
        seen.add(case_id)
        expect = item.get("expect")
        if expect is None:
            expect = []
        if not isinstance(expect, list):
            raise ValueError(f"{case_id} 的 expect 必须是列表")
        cases.append(
            Case(
                id=case_id,
                query=str(item["query"]),
                expect=tuple(str(citation) for citation in expect),
                known_fail=bool(item.get("known_fail", False)),
            )
        )
    return cases


def evaluate(
    cases: list[Case],
    *,
    floor: float,
    field_boost: int,
    limit: int = 3,
    knowledge_dir: Path = KNOWLEDGE_DIR,
) -> list[Outcome]:
    """用一组参数跑完所有用例。

    每次重新建索引而不是复用 ``load_knowledge_base`` 的缓存：
    sweep 要换 ``field_boost``，缓存里的索引是带参的，复用会让整格结果失真。
    """
    chunks: list[Chunk] = []
    if knowledge_dir.is_dir():
        for path in sorted(knowledge_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in (".md", ".markdown", ".txt"):
                source = str(path.relative_to(knowledge_dir)).replace("\\", "/")
                chunks.extend(split_markdown(source, path.read_text(encoding="utf-8")))
    index = BM25Index(chunks, field_boost=field_boost)

    outcomes: list[Outcome] = []
    for case in cases:
        hits = tuple(hit.chunk.citation for hit in index.search(case.query, limit, floor=floor))
        outcomes.append(Outcome(case=case, hits=hits))
    return outcomes


@dataclass(frozen=True)
class Metrics:
    """两套数：``全部`` 和 ``基线内``。

    只报基线内会让报告变成自我欺骗——把两条真没命中的用例标成 known_fail 之后
    recall@3 就变成 1.00，看起来完美。所以头行报**全部**（诚实），
    第二行才报扣掉已知未命中的基线（用来判断「有没有新增回归」）。
    """

    total: int
    recalled_1: int
    recalled_3: int
    mrr: float
    baseline_total: int
    baseline_recalled_3: int
    negative_total: int
    negative_ok: int
    known_total: int
    known_hit: int

    @property
    def recall_at_1(self) -> float:
        return self.recalled_1 / self.total if self.total else 0.0

    @property
    def recall_at_3(self) -> float:
        return self.recalled_3 / self.total if self.total else 0.0

    @property
    def baseline_recall_at_3(self) -> float:
        return self.baseline_recalled_3 / self.baseline_total if self.baseline_total else 1.0

    @property
    def negative_rate(self) -> float:
        return self.negative_ok / self.negative_total if self.negative_total else 1.0


def summarize(outcomes: list[Outcome]) -> Metrics:
    """汇总指标。正例分两套算：全部、以及扣掉 ``known_fail`` 的基线。"""
    positive = [o for o in outcomes if not o.case.is_negative]
    baseline = [o for o in positive if not o.case.known_fail]
    negative = [o for o in outcomes if o.case.is_negative]
    known = [o for o in positive if o.case.known_fail]
    rr = [1.0 / (o.first_hit_rank + 1) for o in positive if o.first_hit_rank is not None]
    return Metrics(
        total=len(positive),
        recalled_1=sum(1 for o in positive if o.hit_at_1),
        recalled_3=sum(1 for o in positive if o.hit_at_3),
        mrr=(sum(rr) / len(positive)) if positive else 0.0,
        baseline_total=len(baseline),
        baseline_recalled_3=sum(1 for o in baseline if o.hit_at_3),
        negative_total=len(negative),
        negative_ok=sum(1 for o in negative if o.negative_ok),
        known_total=len(known),
        known_hit=sum(1 for o in known if o.hit_at_3),
    )


def format_report(outcomes: list[Outcome], metrics: Metrics) -> str:
    lines = [
        f"用例 {len(outcomes)} 条（正例 {metrics.total}，负例 {metrics.negative_total}，"
        f"其中已知未命中 {metrics.known_total}）",
        f"recall@1 = {metrics.recall_at_1:.2f}   recall@3 = {metrics.recall_at_3:.2f}   "
        f"MRR = {metrics.mrr:.2f}   负例返回空 = {metrics.negative_rate:.2f}",
        f"扣掉 {metrics.known_total} 条已知未命中后：recall@3 = "
        f"{metrics.baseline_recall_at_3:.2f}（这条是基线，新增回归看它）",
        f"已知未命中里已经能命中的：{metrics.known_hit}/{metrics.known_total}"
        "（有的话把 yaml 里的 known_fail 删掉）",
        "",
    ]
    for outcome in outcomes:
        if outcome.case.is_negative:
            mark = "ok " if outcome.negative_ok else "!! "
            first = outcome.hits[0] if outcome.hits else "（空）"
            lines.append(f"{mark}{outcome.case.id:<28} {outcome.case.query} -> {first}")
            continue
        mark = "ok " if outcome.hit_at_3 else ("?? " if outcome.case.known_fail else "!! ")
        first = outcome.hits[0] if outcome.hits else "（空）"
        lines.append(f"{mark}{outcome.case.id:<28} {outcome.case.query}")
        lines.append(f"    期望 {list(outcome.case.expect)}")
        lines.append(f"    实际 {first}")
    return "\n".join(lines)


def _fail_under(metrics: Metrics, threshold: float) -> bool:
    """``--fail-under`` 判定：正例 recall@3 和负例空率都要过线。"""
    return metrics.recall_at_3 >= threshold and metrics.negative_rate >= 1.0


def sweep(cases: list[Case]) -> None:
    """扫 RELATIVE_FLOOR × FIELD_BOOST 网格，看哪一格把什么弄坏了。

    调参要看全表，不是只看总分：某一格总分高但把某条查询弄坏，
    那条查询往往在总分里被平均掉。
    """
    floors = (0.20, 0.25, 0.30, 0.35, 0.40, 0.45)
    boosts = (1, 2, 3, 4)
    header = "floor\\boost " + " ".join(f"{b:>20}" for b in boosts)
    print(header)
    for floor in floors:
        cells: list[str] = []
        for boost in boosts:
            outcomes = evaluate(cases, floor=floor, field_boost=boost)
            metrics = summarize(outcomes)
            # r@1 单独列出来：只看 r@3 会漏掉「对的小节掉到第 2、3 位」，
            # 而模型实际只读前几条，排序变差是能感觉到的
            cells.append(
                f"r1 {metrics.recall_at_1:.2f} r3 {metrics.recall_at_3:.2f} "
                f"n {metrics.negative_rate:.2f}"
            )
        print(f"{floor:<11.2f} " + " ".join(f"{cell:>20}" for cell in cells))
    print("\n单元格：r1 = recall@1，r3 = recall@3，n = 负例返回空的比例")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument("--fail-under", type=float, default=0.0)
    parser.add_argument("--floor", type=float, default=None, help="覆盖默认相对阈值")
    parser.add_argument("--boost", type=int, default=None, help="覆盖默认字段加权")
    parser.add_argument("--sweep", action="store_true")
    args = parser.parse_args()

    cases = load_cases()
    if args.limit:
        cases = cases[: args.limit]

    if args.sweep:
        sweep(cases)
        return 0

    floor = args.floor if args.floor is not None else RELATIVE_FLOOR
    boost = args.boost if args.boost is not None else FIELD_BOOST

    outcomes = evaluate(cases, floor=floor, field_boost=boost)
    metrics = summarize(outcomes)
    print(format_report(outcomes, metrics))

    if args.fail_under and not _fail_under(metrics, args.fail_under):
        print(f"\n未过线：要求 recall@3 >= {args.fail_under} 且负例全空")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
