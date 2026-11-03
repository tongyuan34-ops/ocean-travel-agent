"""RAG 检索层：分词、切块、BM25、语料加载。

这些用例盯的都是「不报错的错」：检索永远能返回点什么，所以「返回了但不对」
只能靠断言具体内容来抓。
"""

from __future__ import annotations

from pathlib import Path

from app.rag import BM25Index, get_knowledge_base, load_knowledge_base, reset_cache, tokenize
from app.rag.chunk import Chunk, split_markdown

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"


# ---------- 分词 ----------


def test_chinese_is_split_into_bigrams():
    """中文按二元组切：保证任何两字组合都能命中，召回比精确切词稳。"""
    assert tokenize("湄洲岛") == ["湄洲", "洲岛"]


def test_single_chinese_char_survives():
    """单字查询在纯二元组下会全灭，所以长度 1 的串要原样保留。"""
    assert tokenize("船") == ["船"]


def test_ascii_and_chinese_do_not_bleed():
    assert tokenize("gpt-4 模型") == ["gpt-4", "模型"]


def test_punctuation_and_emoji_are_separators():
    """标点断开的是「词边界」，所以不会跨标点产生二元组。"""
    assert tokenize("预算，怎么分项！🌊") == ["预算", "么分", "分项"]


def test_stopwords_are_dropped():
    """虚词在小语料里 IDF 反而高，会把主题词压下去，必须过滤。"""
    assert "怎么" not in tokenize("预算怎么分项")
    assert "预算" in tokenize("预算怎么分项")


def test_quantity_bigrams_are_dropped():
    """数词+量词的二元组没有主题内容，但能独立命中。

    真踩过：领域外的问题「帮我写一份周报」靠「一份」命中了预算文档，
    返回一条不相关的规则——工具说「查到了」，模型就拿它当依据。
    """
    assert "一份" not in tokenize("帮我写一份周报")
    assert "三个" not in tokenize("三个景点怎么排")
    assert "两种" not in tokenize("两种方案选哪个")


def test_quantity_rule_only_drops_pure_quantity_pairs():
    """只丢「两个字都是数量词」的组合。

    「两天」「几天」带主题字（天数本身就是行程里的关键变量），
    「几点」「多少元」里的 点/元 也不是量词——这些误伤会直接把正常查询切坏。
    """
    assert "两天" in tokenize("两天够不够")
    assert "几天" in tokenize("去几天合适")
    assert "几点" in tokenize("景点几点关门")
    assert "少元" in tokenize("预算多少元")


def test_fullwidth_is_normalized():
    assert tokenize("ＧＰＴ") == tokenize("gpt")


# ---------- 切块 ----------


def test_sections_become_separate_chunks():
    text = "# 总则\n\n第一条。\n\n# 预算\n\n第二条。"
    chunks = split_markdown("a.md", text)

    assert [chunk.heading for chunk in chunks] == ["总则", "预算"]
    assert chunks[0].text == "第一条。"


def test_chunk_ids_are_stable_and_sequential():
    """id 必须可复现：引用要能回溯到具体块，否则「生成的东西无法验收」。"""
    text = "# A\n\n一。\n\n# B\n\n二。"
    first = [chunk.id for chunk in split_markdown("doc.md", text)]
    second = [chunk.id for chunk in split_markdown("doc.md", text)]

    assert first == second == ["doc.md#0", "doc.md#1"]


def test_long_section_is_packed_with_overlap():
    """超长小节要切开，但相邻块之间要留重叠，避免一句话被拦腰截断。"""
    paragraphs = "\n\n".join(f"第{i}段" + "字" * 80 for i in range(6))
    chunks = split_markdown("a.md", "# 长\n\n" + paragraphs, max_chars=200, overlap=40)

    assert len(chunks) > 1
    # 第二块的开头应该来自第一块的结尾（重叠部分）
    assert chunks[1].text[:20] in chunks[0].text


def test_citation_includes_source_and_heading():
    chunk = Chunk(id="a.md#0", source="a.md", heading="预算", text="x")
    assert chunk.citation == "a.md › 预算"


# ---------- BM25 ----------


def _index() -> BM25Index:
    chunks = [
        Chunk(id="a#0", source="a.md", heading="预算", text="预算要分项列出交通住宿门票"),
        Chunk(id="b#0", source="b.md", heading="轮渡", text="末班船时间必须写进行程"),
        Chunk(id="c#0", source="c.md", heading="节奏", text="单日景点不超过四个"),
    ]
    return BM25Index(chunks)


def test_search_finds_the_right_chunk():
    hits = _index().search("预算怎么分项", 1)

    assert hits
    assert hits[0].chunk.source == "a.md"


def test_unrelated_query_returns_nothing():
    """查不到就返回空，让工具明确说「没有相关条目」。

    中文二元组下几乎任何查询都能蹭到几分，不设阈值的话模型会拿不相关的
    规则当依据——这比「查不到」危险得多。
    """
    assert _index().search("量子力学", 3) == []


def test_scores_are_sorted_desc():
    hits = _index().search("行程 预算", 3)

    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)


def test_idf_is_always_positive():
    """经典公式在 df > N/2 时会给负分，短语料里很容易踩到。"""
    index = BM25Index(
        [
            Chunk(id="a#0", source="a.md", heading="", text="行程"),
            Chunk(id="b#0", source="b.md", heading="", text="行程"),
            Chunk(id="c#0", source="c.md", heading="", text="行程"),
        ]
    )
    assert index._idf("行程") > 0


def test_empty_index_is_safe():
    assert BM25Index([]).search("任何", 3) == []


# ---------- 语料加载 ----------


def test_missing_directory_yields_empty_base(tmp_path: Path):
    base = load_knowledge_base(tmp_path / "nope")

    assert base.size == 0
    assert base.search("预算", 3) == []


def test_unreadable_file_does_not_empty_the_base(tmp_path: Path):
    (tmp_path / "ok.md").write_text("# 预算\n\n预算要分项。", encoding="utf-8")
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe\x00broken")

    base = load_knowledge_base(tmp_path)

    assert base.size >= 1


def test_cache_rebuilds_when_file_changes(tmp_path: Path):
    """开发时会改语料，不能要求重启进程才生效。"""
    path = tmp_path / "a.md"
    path.write_text("# 预算\n\n第一条。", encoding="utf-8")
    reset_cache()

    first = get_knowledge_base(tmp_path).size
    path.write_text("# 预算\n\n第一条。\n\n# 轮渡\n\n第二条。", encoding="utf-8")
    second = get_knowledge_base(tmp_path).size

    assert second > first


def test_repo_corpus_recall_at_three():
    """仓库自带的语料在 ``load_knowledge_base`` 这条公开路径上也能查准。

    完整的检索回归集在 ``tests/test_rag_recall.py``（30 条正例 + 5 条负例，
    小节级标注）。这里只留三条冒烟：它们走的是 ``load_knowledge_base`` 的
    缓存路径，而回归集为了能换参数是自己建索引的——两条路径都得有人盯着。
    """
    cases = [
        ("一天安排几个景点", "10-行程节奏.md"),
        ("末班船赶不上", "40-海岛与轮渡.md"),
        ("雨天怎么改行程", "50-雨天与备选方案.md"),
    ]
    reset_cache()
    base = load_knowledge_base(KNOWLEDGE_DIR)
    assert base.size > 0

    missed = [
        query
        for query, expected in cases
        if expected not in [h.chunk.source for h in base.search(query, 3)]
    ]
    assert not missed, f"这些查询没检索到预期文档：{missed}"
