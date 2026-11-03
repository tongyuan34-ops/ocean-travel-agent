"""知识库加载：从目录读语料到可检索对象。

**知识库存什么、不存什么——这条边界是这个模块存在的理由**

存：**规则与判断标准**。比如「带学龄前儿童时单日景点不超过 3 个」「预算要给
门票和市内交通各留一档」「末班船时间必须以当天公告为准」。这些是稳定的、
可以由人写下来并审核的。

不存：**实时事实**。轮渡班次、门票价格、天气、营业时间。这些会变，写进知识库
就是在制造「看起来很权威的过期信息」。它们必须走工具查（``plan_route`` /
``get_weather`` / ``search_web``）。

这条边界写在语料的第一篇里，也在工具描述里重复了一遍——因为模型只能看到
工具描述，看不到这个文件的注释。

**缓存策略**

语料是随仓库分发的静态文件，但开发时会改。所以缓存键里带上「所有文件的
路径 + 修改时间」，改一个文件就自动重建，不用重启进程。语料几十个文件时，
这个扫描是微秒级的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.rag.chunk import Chunk, split_markdown
from app.rag.index import BM25Index, Hit

logger = logging.getLogger(__name__)

# 只收这两种：知识库是给人写的，不处理 pdf/docx（那属于上传链路）
_SUFFIXES = (".md", ".markdown", ".txt")


@dataclass(frozen=True)
class KnowledgeBase:
    chunks: list[Chunk]
    index: BM25Index

    @property
    def size(self) -> int:
        return len(self.chunks)

    def search(self, query: str, limit: int = 4) -> list[Hit]:
        return self.index.search(query, limit)


def _signature(root: Path) -> tuple[tuple[str, float], ...]:
    """目录指纹：路径 + 修改时间。用来判断缓存是否还有效。"""
    items: list[tuple[str, float]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SUFFIXES:
            try:
                items.append((str(path.relative_to(root)), path.stat().st_mtime))
            except OSError:
                # 文件刚好被删/锁住：跳过，不阻断整体加载
                continue
    return tuple(items)


def load_knowledge_base(directory: str | Path) -> KnowledgeBase:
    """读目录下所有 md/txt，切块并建索引。目录不存在时返回空库。"""
    root = Path(directory)
    if not root.is_dir():
        logger.warning("知识库目录不存在：%s（search_knowledge 将返回空结果）", root)
        return KnowledgeBase(chunks=[], index=BM25Index([]))

    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 单个文件读不了不该让整个知识库为空
            logger.warning("跳过无法读取的知识文件 %s：%s", path, exc)
            continue
        source = str(path.relative_to(root)).replace("\\", "/")
        chunks.extend(split_markdown(source, text))

    logger.info("知识库已加载：%d 个文件片段，来源 %s", len(chunks), root)
    return KnowledgeBase(chunks=chunks, index=BM25Index(chunks))


class KnowledgeStore:
    """带失效判断的缓存。目录改了自动重建，没改直接复用。"""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._signature: tuple[tuple[str, float], ...] = ()
        self._base: KnowledgeBase | None = None

    def get(self, directory: str | Path) -> KnowledgeBase:
        root = Path(directory)
        signature = _signature(root)
        if self._base is not None and root == self._root and signature == self._signature:
            return self._base
        self._root = root
        self._signature = signature
        self._base = load_knowledge_base(root)
        return self._base

    def reset(self) -> None:
        self._root = None
        self._signature = ()
        self._base = None


# 进程级单例：工具是模块级函数，没有地方挂实例
_store = KnowledgeStore()


def get_knowledge_base(directory: str | Path) -> KnowledgeBase:
    return _store.get(directory)


def reset_cache() -> None:
    """给测试用：清掉缓存，避免上一个用例的语料漏到下一个。"""
    _store.reset()
