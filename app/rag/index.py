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
2. **相对阈值**：只保留得分不低于最高分 35% 的结果。中文二元组会让几乎任何
   查询都命中几个块，不过滤的话「今天天气怎么样」也能检索出三条行程规则，
   模型就会拿不相关的内容当依据。宁可返回空，让工具明确说「没有相关条目」。
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
# 文件名/标题命中一次算两次
FIELD_BOOST = 2


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class BM25Index:
    """在内存里建倒排索引。语料是几十到几百块，构建一次不到毫秒级。"""

    def __init__(self, chunks: list[Chunk], *, k1: float = K1, b: float = B) -> None:
        self._chunks = list(chunks)
        self._k1 = k1
        self._b = b

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
                counts[term] += FIELD_BOOST * count
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

    def search(self, query: str, limit: int = 4) -> list[Hit]:
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
        floor = top * RELATIVE_FLOOR
        return [
            Hit(chunk=self._chunks[doc], score=score)
            for doc, score in ranked[:limit]
            if score >= floor
        ]
