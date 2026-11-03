"""检索回归基线：知识库查得准不准，用带标注的用例集钉住。

和 ``tests/test_rag.py`` 的分工：那个文件验**检索器的行为**（BM25 算得对不对、
切块边界在哪、阈值怎么算），用例是手搓的小语料；这个文件验**当前语料上的
实际效果**，用例是 ``evals/retrieval_cases.yaml`` 里 36 条真实问法。

**为什么失败集合要「恰好等于」声明的 known_fail**

不用 ``recall@3 >= 0.93`` 这种阈值：语料一加就漂，而且漂了之后
「少对一条」和「多对一条」看起来一样。

改成集合相等：

- 新出现一条失败 → 集合变大 → 红。这是回归。
- 某条 known_fail 开始过了 → 集合变小 → 也红，并提示把它从 yaml 里删掉。
  否则基线会慢慢退化成「一串早就不成立的例外」。
- 想让新失败变绿，只能去 yaml 里加 ``known_fail``——这是一次显式编辑，
  评审时看得见。子集断言做不到这一点：加不加都能过。
"""

from __future__ import annotations

from pathlib import Path

from evals.retrieval import KNOWLEDGE_DIR, evaluate, load_cases, summarize

from app.rag.chunk import split_markdown
from app.rag.index import FIELD_BOOST, RELATIVE_FLOOR


def _outcomes():
    return evaluate(load_cases(), floor=RELATIVE_FLOOR, field_boost=FIELD_BOOST)


def _sections() -> set[str]:
    """当前语料里所有「文件 › 小节」。"""
    found: set[str] = set()
    for path in sorted(KNOWLEDGE_DIR.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".md", ".markdown", ".txt"):
            source = str(path.relative_to(KNOWLEDGE_DIR)).replace("\\", "/")
            found.update(
                chunk.citation for chunk in split_markdown(source, path.read_text(encoding="utf-8"))
            )
    return found


def test_retrieval_matches_declared_baseline():
    """正例的失败集合必须和 yaml 里声明的 known_fail 完全一致。"""
    outcomes = _outcomes()
    failed = {o.case.id for o in outcomes if not o.case.is_negative and not o.hit_at_3}
    declared = {o.case.id for o in outcomes if o.case.known_fail}

    newly_broken = failed - declared
    unexpectedly_fixed = declared - failed
    assert not newly_broken, (
        f"这些用例原来能命中，现在不行了：{sorted(newly_broken)}\n"
        f"跑 `python -m evals.retrieval` 看具体命中到了哪一节。"
    )
    assert not unexpectedly_fixed, (
        f"这些用例被标成 known_fail，但其实已经能命中了：{sorted(unexpectedly_fixed)}\n"
        f"把 yaml 里的 known_fail 删掉，让基线跟上实现。"
    )


def test_out_of_domain_queries_return_nothing():
    """领域外的问题必须返回空，不能硬凑三条不相关的规则给模型当依据。"""
    outcomes = _outcomes()
    negatives = [o for o in outcomes if o.case.is_negative]
    assert negatives, "用例集里没有负例，阈值调过头也没人发现"

    leaked = {o.case.id: o.hits for o in negatives if not o.negative_ok}
    assert not leaked, f"这些领域外问题命中了知识库：{leaked}"


def test_every_knowledge_section_is_covered_by_a_case():
    """语料里的每个小节都要有对应用例，且用例引用的节必须真实存在。

    加一篇文档却忘了标用例，检索效果就少一块被测面积，而且没人会注意到——
    这条让「加了文档」变成一个必须处理的事。
    """
    covered = {citation for case in load_cases() for citation in case.expect}
    sections = _sections()

    unlabelled = sections - covered
    dangling = covered - sections
    assert not unlabelled, f"这些小节没有对应用例：{sorted(unlabelled)}"
    assert not dangling, f"这些用例引用了不存在的小节：{sorted(dangling)}"


def test_metrics_are_not_secretly_terrible():
    """留一条粗线：准确率整体掉到 0.8 以下，就不是「个别用例」的问题了。

    上面几条盯单条用例，这条盯大盘。故意写得很松——它拦的是「索引整个坏掉」
    （比如分词全挂、索引建空），不是拦正常波动。
    """
    metrics = summarize(_outcomes())
    assert metrics.recall_at_3 >= 0.8, metrics
    assert metrics.negative_rate >= 0.8, metrics


def test_case_file_is_not_empty():
    """用例文件读成空列表时，上面所有断言都会「通过」。"""
    cases = load_cases()
    assert len(cases) >= 20
    assert sum(1 for case in cases if case.is_negative) >= 3


def test_knowledge_dir_points_at_repo_corpus():
    """评测必须跑在仓库里的语料上，不能跟着运行环境的配置漂。"""
    assert Path(KNOWLEDGE_DIR).is_dir()
    assert (Path(KNOWLEDGE_DIR) / "10-行程节奏.md").is_file()
