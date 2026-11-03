"""BM25 检索：纯 Python，无第三方依赖。

**为什么不上向量库**

知识库是几十个短文档、语料随时可读，检索延迟要求是「一次工具调用内完成」。
这种规模下 BM25 的召回和向量检索差距很小，而代价差一个量级：
向量要额外装 embedding 依赖、要建索引文件、要在测试里 mock 掉网络。
这个项目的测试全部离线可跑，加一个需要联网建索引的组件会破坏这个性质。

**这里做对的三个细节**

1. ``idf`` 用 ``log(1 + (N - df + 0.5) / (df + 0.5))``：恒正。
   经典公式在 df > N/2 时会给负值，导致「含这个词的文档反而被扣分」——
   短语料里非常容易踩到。
2. **相对阈值**：只保留得分不低于最高分 35% 的结果。它管的是**尾部**——把
   「勉强沾边」的条目砍掉，而不是判断「有没有相关结果」：最高分永远能过线
   （``top >= top * floor``），所以阈值调多高都不会让一次命中变成空结果。
   能不能返回空，取决于分词后有没有 term 真的命中——``今天股市怎么样``
   返回空，靠的是虚词和数量词被过滤掉，不是靠这个阈值。
   （``evals/retrieval.py --sweep`` 实测：0.20～0.55 之间结果完全一样，
   0.60 开始才伤到 recall@3。）
3. **稳定排序**：同分按块 id 排。检索结果必须可复现，否则测试和线上对不上。
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from app.rag.chunk import Chunk
from app.rag.tokenize import tokenize

K1 = 1.5
B = 0.75
# 低于最高分这个比例的结果直接丢弃
RELATIVE_FLOOR = 0.35
# 文件名/标题命中一次算两次。
#
# 这个值不是拍的：``evals/retrieval.py --sweep`` 在 31 条带标注查询上扫过
# 1/2/3/4 四档。recall@3 全档一样（0.90），但 recall@1 是 0.65 / 0.74 /
# 0.77 / 0.77——boost=3 把「带两岁小孩一天能去几个景点」从「通勤缓冲」
# 拉回「节奏比景点重要」，且没有任何一条变差。取 3（到 3 就饱和了，取 4
# 没有额外收益，只是把标题权重推得更高）。
FIELD_BOOST = 3


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class BM25Index:
    """在内存里建倒排索引。语料是几十到几百块，构建一次不到毫秒级。"""

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        k1: float = K1,
        b: float = B,
        field_boost: int = FIELD_BOOST,
    ) -> None:
        self._chunks = list(chunks)
        self._k1 = k1
        self._b = b
        self._field_boost = field_boost

        postings: dict[str, dict[int, int]] = {}
        lengths: list[int] = []
        for index, chunk in enumerate(self._chunks):
            # 字段加权（BM25F 的简化版）：文件名和标题里出现的词权重加倍。
            # 用户提问用的词往往正是它们（「亲子」「预算」「雨天」），
            # 而正文里可能写成「学龄前儿童」——不加权的话正文长的块会赢。
            field_terms = tokenize(f"{chunk.source} {chunk.heading}")
            body_terms = tokenize(chunk.text)
            counts: Counter[str] = Counter(body_terms)
            for term, count in Counter(field_terms).items():
                counts[term] += self._field_boost * count
            lengths.append(len(body_terms) + len(field_terms))
            for term, count in counts.items():
                postings.setdefault(term, {})[index] = count

        self._postings = postings
        self._lengths = lengths
        self._avg_length = (sum(lengths) / len(lengths)) if lengths else 0.0
        self._total = len(self._chunks)

    @property
    def size(self) -> int:
        return self._total

    def _idf(self, term: str) -> float:
        df = len(self._postings.get(term, ()))
        if df == 0:
            return 0.0
        return math.log(1 + (self._total - df + 0.5) / (df + 0.5))

    def search(self, query: str, limit: int = 4, *, floor: float = RELATIVE_FLOOR) -> list[Hit]:
        """返回命中的块，按得分降序。查不到相关条目时返回空列表。"""
        if not self._chunks or limit <= 0:
            return []
        terms = tokenize(query)
        if not terms:
            return []

        scores: dict[int, float] = {}
        for term in set(terms):
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = self._idf(term)
            for doc, tf in posting.items():
                length = self._lengths[doc] or 1
                denominator = tf + self._k1 * (
                    1 - self._b + self._b * length / (self._avg_length or 1)
                )
                scores[doc] = scores.get(doc, 0.0) + idf * tf * (self._k1 + 1) / denominator

        if not scores:
            return []
        ranked = sorted(scores.items(), key=lambda item: (-item[1], self._chunks[item[0]].id))
        top = ranked[0][1]
        cutoff = top * floor
        return [
            Hit(chunk=self._chunks[doc], score=score)
            for doc, score in ranked[:limit]
            if score >= cutoff
        ]
