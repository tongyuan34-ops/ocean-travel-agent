"""文档编辑：**只写副本**，永不改原文件。

**为什么强制副本**

编辑是不可逆的：一次替换写错、样式被压平、表格被重排，原件就回不去了。而模型
做替换时最容易犯的错恰好是「匹配太宽」——把「甲方」替换成公司名，结果把
「甲方代表」也改了。生成副本之后，最坏结果是多一个文件，用户对照着看一遍再决定
要不要用；覆盖原件的方案里，最坏结果是资料没了。

所以这里没有「覆盖」这个选项，连参数都不提供。

**为什么 docx 的替换要按「整段拼接」处理**

Word 会把 ``{{客户名}}`` 拆成三个 run（``{{``、``客户名``、``}}``），原因是用户
编辑时改过格式或输入法切换过。逐 run 匹配永远找不到这种占位符，而它在真实
文档里极其常见。所以这里把整段的 run 拼起来替换，再写回第一个 run。

代价：**这一段内部的局部格式会被抹平**（半句加粗没了）。占位符场景下没有
格式可保留，所以这个代价可以接受；但如果一段里既有占位符又有需要保留的
局部格式，结果会不好看——这条写进 ``notes`` 里，让模型知道发生了什么。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.docs.reader import DOCX_SUFFIXES, XLSX_SUFFIXES, DocError

# 副本后缀。用中文是因为它最终会出现在用户的文件管理器里
COPY_SUFFIX = "-已编辑"


@dataclass
class EditOutcome:
    output: Path
    applied: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"已生成副本：{self.output}"]
        if self.applied:
            lines.append(f"生效 {len(self.applied)} 项：" + "；".join(self.applied))
        if self.missed:
            lines.append("没找到（原文里可能没有这段文字）：" + "；".join(self.missed))
        if self.notes:
            lines.extend(f"说明：{note}" for note in self.notes)
        lines.append("原文件未改动。确认无误后再自行替换或继续编辑。")
        return "\n".join(lines)


def free_path(path: Path) -> Path:
    """同名文件已存在就加序号：``报告.xlsx`` → ``报告2.xlsx``。绝不覆盖。

    覆盖是最省事也最不可逆的选择：用户上一版结果还在，重跑一次就没了。
    加序号最多让他多看一眼文件名，代价小得多。
    """
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def copy_target(path: Path, save_as: Path | None = None) -> Path:
    """``合同.docx`` → ``合同-已编辑.docx``；已存在就加序号，绝不覆盖。

    ``save_as`` 是用户指定的完整输出路径（桌面、下载…，名字也是他起的）。
    没给就留在源文件旁边——结果跑到一个用户不知道的目录里，等于没生成。
    """
    if save_as is not None:
        return free_path(save_as)
    return free_path(path.with_name(f"{path.stem}{COPY_SUFFIX}{path.suffix}"))


def _require_operations(operations: list[dict[str, Any]]) -> None:
    if not operations:
        raise DocError("没有给出任何编辑操作")


def _iter_docx_paragraphs(document: Any) -> list[Any]:
    """正文 + 表格单元格 + 页眉页脚。

    合同里的甲方乙方经常在页眉，只在正文里替换会漏掉一半。
    """
    from docx.table import Table

    paragraphs: list[Any] = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
                # 嵌套表格（单元格里还有表）只往下探一层，够用且不会死循环
                for nested in cell.tables:
                    for nested_row in nested.rows:
                        for nested_cell in nested_row.cells:
                            paragraphs.extend(nested_cell.paragraphs)
    for section in document.sections:
        paragraphs.extend(section.header.paragraphs)
        paragraphs.extend(section.footer.paragraphs)
    # Table 只是为了让 mypy 知道 import 有用；运行时不需要
    del Table
    return paragraphs


def _replace_in_paragraph(paragraph: Any, find: str, replace: str) -> int:
    """返回替换次数；``-1`` 表示文字在 runs 之外（超链接/域），改不了。"""
    runs = paragraph.runs
    joined = "".join(run.text or "" for run in runs)
    if find not in joined:
        if find in (paragraph.text or ""):
            return -1
        return 0

    count = joined.count(find)
    new_text = joined.replace(find, replace)
    if runs:
        runs[0].text = new_text
        for run in runs[1:]:
            run.text = ""
    else:  # pragma: no cover - 有文字就一定有 run，这里只是兜底
        paragraph.add_run(new_text)
    return count


def _edit_docx(path: Path, operations: list[dict[str, Any]], outcome: EditOutcome) -> None:
    from docx import Document

    document = Document(str(path))
    paragraphs = _iter_docx_paragraphs(document)

    for index, operation in enumerate(operations, start=1):
        kind = str(operation.get("op") or "").strip()
        if kind == "replace":
            find = str(operation.get("find") or "")
            replacement = str(operation.get("replace") or "")
            if not find:
                outcome.missed.append(f"第 {index} 项 replace 缺少 find")
                continue
            total = 0
            locked = 0
            for paragraph in paragraphs:
                result = _replace_in_paragraph(paragraph, find, replacement)
                if result < 0:
                    locked += 1
                elif result > 0:
                    total += result
            if total:
                outcome.applied.append(f"替换「{find}」{total} 处")
            else:
                outcome.missed.append(f"「{find}」")
            if locked:
                outcome.notes.append(
                    f"有 {locked} 处「{find}」在超链接或域里，改不了，需要用户手动处理"
                )
            if total:
                outcome.notes.append("被替换的段落内部局部格式（如半句加粗）已合并为一种格式")
        elif kind == "append":
            text = str(operation.get("text") or "")
            if not text:
                outcome.missed.append(f"第 {index} 项 append 缺少 text")
                continue
            style = str(operation.get("style") or "").strip()
            try:
                document.add_paragraph(text, style=style or None)
            except (KeyError, ValueError):
                document.add_paragraph(text)
                outcome.notes.append(f"样式「{style}」不存在，已按正文追加")
            outcome.applied.append(f"追加段落「{text[:20]}」")
        else:
            outcome.missed.append(f"第 {index} 项操作「{kind}」不适用于 docx")

    document.save(str(outcome.output))


def _edit_xlsx(path: Path, operations: list[dict[str, Any]], outcome: EditOutcome) -> None:
    from openpyxl import load_workbook

    workbook = load_workbook(filename=str(path))
    try:
        for index, operation in enumerate(operations, start=1):
            kind = str(operation.get("op") or "").strip()
            sheet_name = str(operation.get("sheet") or "").strip()
            if sheet_name and sheet_name not in workbook.sheetnames:
                outcome.missed.append(f"第 {index} 项：没有工作表「{sheet_name}」")
                continue
            sheet = workbook[sheet_name] if sheet_name else workbook.active

            if kind == "set_cell":
                cell = str(operation.get("cell") or "").strip().upper()
                if not cell:
                    outcome.missed.append(f"第 {index} 项 set_cell 缺少 cell")
                    continue
                sheet[cell] = operation.get("value")
                outcome.applied.append(f"{sheet.title}!{cell} = {operation.get('value')}")
            elif kind == "append_row":
                values = operation.get("values")
                if not isinstance(values, list) or not values:
                    outcome.missed.append(f"第 {index} 项 append_row 缺少 values 数组")
                    continue
                sheet.append(values)
                outcome.applied.append(f"{sheet.title} 追加一行（{len(values)} 列）")
            else:
                outcome.missed.append(f"第 {index} 项操作「{kind}」不适用于 xlsx")

        workbook.save(str(outcome.output))
    finally:
        workbook.close()


def apply_edits(
    path: Path, operations: list[dict[str, Any]], save_as: Path | None = None
) -> EditOutcome:
    """按操作列表编辑，结果写到副本。原文件只读不写。

    ``save_as`` 是用户指定的输出路径：他说「存到桌面」时传它，否则副本落在
    源文件旁边。
    """
    _require_operations(operations)
    if not path.is_file():
        raise DocError(f"文件不存在：{path}")

    suffix = path.suffix.lower()
    if suffix not in DOCX_SUFFIXES | XLSX_SUFFIXES:
        raise DocError(
            f"只能编辑 .docx 和 .xlsx（{suffix or '无扩展名'} 不支持）。"
            "PDF 没有可编辑的文本流，需要重新生成而不是修改。"
        )

    outcome = EditOutcome(output=copy_target(path, save_as))
    try:
        if suffix in DOCX_SUFFIXES:
            _edit_docx(path, operations, outcome)
        else:
            _edit_xlsx(path, operations, outcome)
    except DocError:
        raise
    except Exception as exc:
        raise DocError(f"编辑失败：{exc}") from exc

    if not outcome.applied:
        # 一项都没生效就别留个空副本在用户目录里
        if outcome.output.exists():
            outcome.output.unlink()
        raise DocError(
            "所有操作都没生效，未生成副本。没找到的内容："
            + ("；".join(outcome.missed) or "（未提供有效操作）")
        )
    return outcome
