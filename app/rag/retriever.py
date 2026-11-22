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
路径 + 大小 + 修改时间」，改一个文件就自动重建，不用重启进程。语料几十个文件时，
这个扫描是微秒级的。

**两级缓存**

- 进程内：``KnowledgeStore`` 记住上一次的指纹和对象。
- 磁盘上：语料超过 ``_DISK_CACHE_MIN_BYTES`` 才落盘。几十 KB 的小语料
  重建只要几毫秒，落盘反而是多了写文件 + 反序列化两道开销，没有收益；
  十万块那种分钟级的构建才值得缓存。落盘文件名就是目录指纹的哈希，
  语料一变指纹就变，旧文件自动失效（并顺手清理），不需要额外的版本管理。
"""

from __future__ import annotations

import hashlib
import logging
import pickle
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import PROJECT_ROOT
from app.rag.chunk import Chunk, split_markdown
from app.rag.index import BM25Index, Hit

logger = logging.getLogger(__name__)

# 只收这两种：知识库是给人写的，不处理 pdf/docx（那属于上传链路）
_SUFFIXES = (".md", ".markdown", ".txt")

# 小于这个体量的语料不落盘：构建耗时低于一次磁盘往返
_DISK_CACHE_MIN_BYTES = 1_000_000
# 默认缓存目录：可写根目录下的 data/index（data/ 已在 .gitignore 里）
_DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "index"


@dataclass(frozen=True)
class KnowledgeBase:
    chunks: list[Chunk]
    index: BM25Index

    @property
    def size(self) -> int:
        return len(self.chunks)

    def search(self, query: str, limit: int = 4) -> list[Hit]:
        return self.index.search(query, limit)


def _signature(root: Path) -> tuple[tuple[str, int, float], ...]:
    """目录指纹：路径 + 大小 + 修改时间。用来判断缓存是否还有效。

    带上大小不只是为了更准：语料批量生成时 mtime 可能撞在同一秒，
    只比 mtime 会把「已经换过内容」的目录误判成没变。
    """
    items: list[tuple[str, int, float]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in _SUFFIXES:
            try:
                stat = path.stat()
            except OSError:
                # 文件刚好被删/锁住：跳过，不阻断整体加载
                continue
            items.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime))
    return tuple(items)


def _iter_sources(root: Path):
    """按稳定顺序产出 (相对路径, 文本)。单个文件读不了就跳过。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 单个文件读不了不该让整个知识库为空
            logger.warning("跳过无法读取的知识文件 %s：%s", path, exc)
            continue
        yield str(path.relative_to(root)).replace("\\", "/"), text


def load_chunks(directory: str | Path) -> list[Chunk]:
    """只切块、不建索引。压测要按不同规模重复建索引，没必要先建一次全量的。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    chunks: list[Chunk] = []
    for source, text in _iter_sources(root):
        chunks.extend(split_markdown(source, text))
    return chunks


def load_knowledge_base(directory: str | Path) -> KnowledgeBase:
    """读目录下所有 md/txt，切块并建索引。目录不存在时返回空库。

    这个函数**总是重建**，不碰磁盘缓存——评测要的就是「从零算一遍」，
    拿缓存跑评测等于没测。
    """
    root = Path(directory)
    if not root.is_dir():
        logger.warning("知识库目录不存在：%s（search_knowledge 将返回空结果）", root)
        return KnowledgeBase(chunks=[], index=BM25Index([]))

    chunks: list[Chunk] = []
    for source, text in _iter_sources(root):
        chunks.extend(split_markdown(source, text))

    logger.info("知识库已加载：%d 个文件片段，来源 %s", len(chunks), root)
    return KnowledgeBase(chunks=chunks, index=BM25Index(chunks))


def _cache_path(signature: tuple[tuple[str, int, float], ...], cache_dir: Path) -> Path:
    digest = hashlib.md5()
    for name, size, mtime in signature:
        digest.update(f"{name}\x00{size}\x00{mtime:.6f}\n".encode())
    return cache_dir / f"kb-{digest.hexdigest()[:16]}.pkl"


def _prune(cache_dir: Path, keep: str) -> None:
    """只留最新一份索引。旧指纹的文件不会再用到，留着就是占几百 MB。"""
    for stale in cache_dir.glob("kb-*.pkl"):
        if stale.name == keep:
            continue
        # 删不掉（被别的进程占着）不该影响本次加载
        with suppress(OSError):
            stale.unlink()


def load_knowledge_base_cached(
    directory: str | Path,
    *,
    cache_dir: str | Path | None = None,
) -> KnowledgeBase:
    """带磁盘缓存的加载：指纹命中就读索引文件，否则重建并落盘。

    任何缓存异常（文件损坏、格式版本不符、目录不可写）都退化成重建，
    不能因为「缓存坏了」让检索整个不可用。
    """
    root = Path(directory)
    if not root.is_dir():
        return load_knowledge_base(root)

    signature = _signature(root)
    total_bytes = sum(size for _, size, _ in signature)
    if total_bytes < _DISK_CACHE_MIN_BYTES:
        # 小语料：毫秒级重建，落盘反而更慢
        return load_knowledge_base(root)

    target_dir = Path(cache_dir) if cache_dir is not None else _DEFAULT_CACHE_DIR
    cache = _cache_path(signature, target_dir)

    if cache.exists():
        try:
            index = BM25Index.load(cache)
            logger.info(
                "知识库索引命中缓存：%d 个片段，%d 字节，来源 %s",
                index.size,
                cache.stat().st_size,
                cache,
            )
            return KnowledgeBase(chunks=index.chunks, index=index)
        except (OSError, ValueError, pickle.UnpicklingError) as exc:
            logger.warning("索引缓存不可用，改为重建：%s", exc)

    base = load_knowledge_base(root)
    # 空语料不落盘：否则每次启动都先读一个空索引再重建
    if base.size:
        try:
            written = base.index.save(cache)
            _prune(target_dir, cache.name)
            logger.info("知识库索引已落盘：%d 个片段，%d 字节，来源 %s", base.size, written, cache)
        except OSError as exc:
            logger.warning("索引落盘失败（不影响本次检索）：%s", exc)
    return base


class KnowledgeStore:
    """带失效判断的缓存。目录改了自动重建，没改直接复用。"""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._signature: tuple[tuple[str, int, float], ...] = ()
        self._base: KnowledgeBase | None = None

    def get(self, directory: str | Path) -> KnowledgeBase:
        root = Path(directory)
        signature = _signature(root)
        if self._base is not None and root == self._root and signature == self._signature:
            return self._base
        self._root = root
        self._signature = signature
        self._base = load_knowledge_base_cached(root)
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
