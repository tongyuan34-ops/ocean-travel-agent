"""文档读取：docx / xlsx / 纯文本 → 「结构块」。

**为什么默认只给一段窗口，而不是全文**

一份 40 页的合同正文三万多字，整段塞进上下文既超预算又没用——模型真正需要的是
「有哪些章节、金额在哪个表、甲方写在哪」。所以这里把文档切成有序的块，默认只返回
前 ``limit`` 块，需要细节时用 ``start`` 继续往后读。这和 ``read_file`` 的
「一次给完再截断」不一样：截断是把尾巴砍掉，分段读是**可以续**的。

**为什么不用 ``document.paragraphs`` 拼正文**

它漏掉表格。而合同、报价单、确认单里的金额、人数、日期几乎全在表格里——
只读段落等于把最关键的信息丢了。所以这里按 ``body`` 的子元素顺序遍历，
段落和表格保持文档里的真实先后。

**为什么表格只给前几十行**

``openpyxl`` 读一个十万行的台账，全量返回会直接撑爆上下文。这里只取表头和
前若干行，并在标题里写明「约 N 行」，让模型知道自己看到的是样本而不是全部。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

# 能读的格式。docx / xlsx 之外都按纯文本处理——用户手上的「文档」经常是
# 导出的 md、csv、txt，不支持反而奇怪
DOCX_SUFFIXES = frozenset({".docx"})
XLSX_SUFFIXES = frozenset({".xlsx", ".xlsm"})
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log"}
)
SUPPORTED_SUFFIXES = DOCX_SUFFIXES | XLSX_SUFFIXES | TEXT_SUFFIXES

# 单个块的字符上限。一个段落两千字，多半是排版问题而不是内容
MAX_BLOCK_CHARS = 500
# 每张表最多读多少行（含表头）
MAX_SHEET_ROWS = 40
# 默认一次返回多少块
DEFAULT_LIMIT = 60


class DocError(Exception):
    """可预期的失败：格式不支持、文件损坏、缺少依赖。

    工具层把它转成 ``DOC_READ_FAILED：...`` / ``DOC_EDIT_FAILED：...``，
    让模型知道「是文档本身的问题」，而不是去重试一次同样的调用。
    """


@dataclass(frozen=True)
class Block:
    """文档里的一段有序内容。``kind`` 决定它在提示里怎么被引用。"""

    kind: str  # heading / paragraph / table / sheet / line
    text: str


@dataclass
class DocText:
    """一次读取的结果。``total`` 是全文块数，``blocks`` 只是窗口。"""

    kind: str
    title: str
    blocks: list[Block]
    total: int
    offset: int
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.blocks) < self.total

    def to_text(self) -> str:
        """渲染成模型能读的文本。标题带层级，表格带行数列数。"""
        lines = [f"《{self.title}》· {self.kind} · 共 {self.total} 块"]
        if self.meta:
            lines.append("　".join(f"{k}={v}" for k, v in self.meta.items()))
        lines.append("")
        for index, block in enumerate(self.blocks, start=self.offset + 1):
            prefix = _KIND_PREFIX.get(block.kind, "")
            lines.append(f"[{index}] {prefix}{block.text}")
        if self.has_more:
            lines.append("")
            lines.append(
                f"…（还有 {self.total - self.offset - len(self.blocks)} 块没读，"
                f"用 start={self.offset + len(self.blocks)} 继续）"
            )
        return "\n".join(lines)


_KIND_PREFIX = {
    "heading": "# ",
    "paragraph": "",
    "table": "",
    "sheet": "",
    "line": "",
}


def _clip(text: str, limit: int = MAX_BLOCK_CHARS) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…（本块还有 {len(flat) - limit} 字）"


def _decode(raw: bytes) -> str:
    """按「UTF-8 优先、GB18030 兜底」解码。

    Windows 上导出的 csv / txt 有相当比例是 GBK。直接 utf-8 会抛异常，
    而 ``errors="replace"`` 会得到一片问号——先试 GB18030 更接近用户预期。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx_blocks(path: Path) -> tuple[list[Block], dict[str, str]]:
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 依赖缺失时才会走到
        raise DocError("缺少 python-docx，无法读取 .docx") from exc

    try:
        document = Document(str(path))
    except Exception as exc:
        raise DocError(f"打不开这个 .docx：{exc}") from exc

    blocks: list[Block] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = (paragraph.text or "").strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
            kind = "heading" if ("heading" in style or style.startswith("标题")) else "paragraph"
            blocks.append(Block(kind=kind, text=_clip(text)))
        elif tag == "tbl":
            table = Table(child, document)
            rows: list[str] = []
            for row in table.rows:
                cells = [" ".join((cell.text or "").split()) for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                head = f"[表格 {len(table.rows)} 行 × {len(table.columns)} 列]"
                blocks.append(
                    Block(kind="table", text=head + "\n" + "\n".join(rows[:MAX_SHEET_ROWS]))
                )

    meta = {
        "段落与表格": str(len(blocks)),
        "节": str(len(document.sections)),
    }
    return blocks, meta


def _xlsx_blocks(path: Path) -> tuple[list[Block], dict[str, str]]:
    """读每个工作表的前若干行。

    **为什么要开两个 workbook**：``data_only=True`` 读的是 Excel 存下来的
    **缓存值**，而缓存只有 Excel / WPS 保存过才有。脚本生成的、或者保存过程
    中断过的文件，公式单元格在这里就是 ``None``——表现是「小计」「合计」
    两列一片空白，模型会以为这些格子本来就没数据。

    所以再开一个 ``data_only=False`` 的副本拿公式原文：有缓存值就用值，
    没有就显示 ``=B2*C2（未计算）``。这样模型能判断「这是公式、只是没算」，
    而不是「这里空着」。代价是多解析一遍——只读前 40 行，可以接受。
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise DocError("缺少 openpyxl，无法读取 .xlsx") from exc

    try:
        cached_book = load_workbook(filename=str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise DocError(f"打不开这个 .xlsx：{exc}") from exc

    try:
        formula_book = load_workbook(filename=str(path), read_only=True, data_only=False)
    except Exception:  # noqa: BLE001 - 拿不到公式就只看缓存值，不该因此读不了文件
        formula_book = None

    blocks: list[Block] = []
    uncached = 0
    try:
        for sheet in cached_book.worksheets:
            raw_sheet = formula_book[sheet.title] if formula_book is not None else None
            raw_rows = raw_sheet.iter_rows(values_only=True) if raw_sheet is not None else None

            rows: list[str] = []
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= MAX_SHEET_ROWS:
                    break
                raw_row = next(raw_rows, None) if raw_rows is not None else None
                cells: list[str] = []
                for position, value in enumerate(row):
                    formula = (
                        raw_row[position]
                        if raw_row is not None and position < len(raw_row)
                        else None
                    )
                    text, was_uncached = _cell_text(value, formula)
                    uncached += 1 if was_uncached else 0
                    cells.append(text)
                if any(cell.strip() for cell in cells):
                    rows.append(" | ".join(cells))

            total_rows = sheet.max_row or 0
            total_cols = sheet.max_column or 0
            head = f"[工作表「{sheet.title}」· 约 {total_rows} 行 × {total_cols} 列]"
            body = "\n".join(rows) if rows else "（空表）"
            blocks.append(Block(kind="sheet", text=f"{head}\n{body}"))
    finally:
        cached_book.close()
        if formula_book is not None:
            formula_book.close()

    meta = {"工作表数": str(len(cached_book.worksheets))}
    if uncached:
        meta["未计算的公式"] = f"{uncached} 格（显示为公式原文）"
    return blocks, meta


def _cell_text(value: object, formula: object) -> tuple[str, bool]:
    """一个单元格的展示文本，以及「它是不是没算过的公式」。"""
    if value is not None:
        return str(value), False
    if isinstance(formula, str) and formula.startswith("="):
        return f"{formula}（未计算）", True
    return "", False


def _text_blocks(path: Path) -> tuple[list[Block], dict[str, str]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DocError(f"读不了这个文件：{exc}") from exc

    text = _decode(raw)
    suffix = path.suffix.lower()
    blocks: list[Block] = []

    if suffix in {".csv", ".tsv"}:
        # csv 按表格渲染：直接给逗号原文，模型会把它当成一句话
        delimiter = "\t" if suffix == ".tsv" else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [" | ".join(cell.strip() for cell in row) for row in reader]
        if rows:
            blocks.append(Block(kind="sheet", text="\n".join(rows[:MAX_SHEET_ROWS])))
        meta = {"行数": str(len(rows))}
        return blocks, meta

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            blocks.append(Block(kind="heading", text=_clip(stripped.lstrip("# ").strip())))
        else:
            blocks.append(Block(kind="line", text=_clip(stripped)))

    meta = {"行数": str(len(blocks)), "字符数": str(len(text))}
    return blocks, meta


def _dispatch(path: Path) -> tuple[list[Block], dict[str, str]]:
    suffix = path.suffix.lower()
    if suffix in DOCX_SUFFIXES:
        return _docx_blocks(path)
    if suffix in XLSX_SUFFIXES:
        return _xlsx_blocks(path)
    if suffix in TEXT_SUFFIXES:
        return _text_blocks(path)
    supported = "、".join(sorted(SUPPORTED_SUFFIXES))
    # 旧版 Office 格式（.xls/.doc/.wps）用 python-docx / openpyxl 都读不了，
    # 光说「不支持」用户不知道下一步该干什么，直接把动作告诉他
    legacy = {".xls", ".doc", ".wps", ".et", ".dps"}
    hint = (
        "。这是 2003 及更早的旧格式，先在 Excel / WPS 里「另存为」.xlsx 或 .docx 再读"
        if suffix in legacy
        else ""
    )
    raise DocError(f"不支持 {suffix or '（无扩展名）'}，当前能读：{supported}{hint}")


def _guess_title(path: Path, blocks: list[Block]) -> str:
    """优先用文档自己的标题，没有再退回文件名。

    「先用文件名」在合同上会得到「扫描件(3).docx」这种没信息量的标题，
    而正文第一个标题通常就是「XX 旅游服务合同」。
    """
    for block in blocks:
        if block.kind == "heading" and block.text:
            return block.text[:60]
    return path.stem


def read_document(path: Path, *, start: int = 0, limit: int = DEFAULT_LIMIT) -> DocText:
    """读一份文档的 [start, start+limit) 块。"""
    if not path.is_file():
        raise DocError(f"文件不存在：{path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        _dispatch(path)  # 抛带支持列表的 DocError

    blocks, meta = _dispatch(path)
    if not blocks:
        raise DocError(f"这份文档里没读到任何内容（可能是空文件或全图片）：{path.name}")

    offset = max(0, start)
    window = blocks[offset : offset + max(1, limit)]
    return DocText(
        kind=path.suffix.lower().lstrip(".") or "text",
        title=_guess_title(path, blocks),
        blocks=window,
        total=len(blocks),
        offset=offset,
        meta=meta,
    )


@dataclass(frozen=True)
class DocDigest:
    """目录概览里的一行：不读正文，只给「值不值得打开」的判断依据。"""

    path: Path
    kind: str
    title: str
    blocks: int
    preview: str

    def to_line(self) -> str:
        # 标题和开头经常是同一句（合同第一行就是标题），重复两遍只是噪音
        tail = "" if self.preview.startswith(self.title) else f"｜{self.preview}"
        return f"- {self.path.name}（{self.kind}，{self.blocks} 块）{self.title}{tail}"


def digest(path: Path) -> DocDigest:
    blocks, _meta = _dispatch(path)
    preview = ""
    for block in blocks:
        if block.text:
            preview = block.text[:60].replace("\n", " ")
            break
    return DocDigest(
        path=path,
        kind=path.suffix.lower().lstrip(".") or "text",
        title=_guess_title(path, blocks),
        blocks=len(blocks),
        preview=preview,
    )


def list_documents(root: Path, *, recursive: bool = True, limit: int = 60) -> list[Path]:
    """列出目录里能读的文档，按修改时间倒序（最近动过的更可能是当前任务）。"""
    pattern = "**/*" if recursive else "*"
    candidates = [
        item
        for item in root.glob(pattern)
        if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[:limit]
