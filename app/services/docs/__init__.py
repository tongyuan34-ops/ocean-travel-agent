"""文档能力：读、编辑（副本）、整理。

分成三个模块而不是一个大文件，因为它们失败的姿势完全不同：

- ``reader``：只读，失败无非是格式不支持或文件损坏，模型换个文件就行；
- ``editor``：写副本，失败要保证「不留半成品」；
- ``organizer``：批量移动，失败必须**在动手之前**就能看出来（所以先出计划）。

对外的出口只有 ``__init__`` 里这几个名字，工具层不直接碰实现细节。
"""

from __future__ import annotations

from app.services.docs.editor import EditOutcome, apply_edits, copy_target
from app.services.docs.organizer import (
    Category,
    Move,
    OrganizePlan,
    apply_plan,
    build_plan,
    parse_categories,
)
from app.services.docs.reader import (
    SUPPORTED_SUFFIXES,
    Block,
    DocDigest,
    DocError,
    DocText,
    digest,
    list_documents,
    read_document,
)
from app.services.docs.sheet import (
    AGG_OPS,
    MAX_DATA_ROWS,
    SHEET_SUFFIXES,
    AggregateResult,
    SheetTable,
    aggregate,
    apply_filters,
    export_copy,
    load_table,
    render_aggregate,
    sort_aggregate,
    sort_rows,
)

__all__ = [
    "AGG_OPS",
    "MAX_DATA_ROWS",
    "SHEET_SUFFIXES",
    "SUPPORTED_SUFFIXES",
    "AggregateResult",
    "Block",
    "Category",
    "DocDigest",
    "DocError",
    "DocText",
    "EditOutcome",
    "Move",
    "OrganizePlan",
    "SheetTable",
    "aggregate",
    "apply_edits",
    "apply_filters",
    "apply_plan",
    "build_plan",
    "copy_target",
    "digest",
    "export_copy",
    "list_documents",
    "load_table",
    "parse_categories",
    "read_document",
    "render_aggregate",
    "sort_aggregate",
    "sort_rows",
]
