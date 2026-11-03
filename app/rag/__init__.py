"""知识库（RAG）：规则类知识的检索层。

分层与 ``selfcheck`` / ``repair`` 一致：这里只做「给定查询，返回相关片段」，
不判断该不该检索、不拼提示词、不碰事件流。

- ``tokenize``：中文二元组分词
- ``chunk``：按标题切块，块 id 稳定可回溯
- ``index``：BM25 + 相对阈值
- ``retriever``：目录加载与失效缓存
"""

from __future__ import annotations

from app.rag.chunk import Chunk, split_markdown
from app.rag.index import BM25Index, Hit
from app.rag.retriever import (
    KnowledgeBase,
    get_knowledge_base,
    load_knowledge_base,
    reset_cache,
)
from app.rag.tokenize import tokenize

__all__ = [
    "BM25Index",
    "Chunk",
    "Hit",
    "KnowledgeBase",
    "get_knowledge_base",
    "load_knowledge_base",
    "reset_cache",
    "split_markdown",
    "tokenize",
]
