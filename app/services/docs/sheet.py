"""表格查询：排序、筛选、导出结果。

**为什么和 ``editor`` 分开**

``editor`` 做的是「把某个单元格改成某个值」——定位是已知的、动作是确定的。
排序和筛选是另一回事：输入是「按金额降序、只要大于 5000 的」，输出是**一批行**，
而且用户八成只是想知道结果，不一定想改文件。混进 ``editor`` 会让那个工具的
参数变成一锅粥，模型也分不清「改一格」和「筛一批」该传什么。

**为什么导出用「新增一个工作表」而不是删掉不匹配的行**

删行是不可逆的，而且 openpyxl 的 ``delete_rows`` 不会调整公式引用——删完之后
「合计」还指着原来的区域，数字就错了。所以导出走「复制整个文件 + 新增一页
『筛选结果』」：原表一个格子都不动，用户能对照着看，公式也不会坏。

**为什么表头要自动识别**

用户不会告诉我们「表头在第 1 行」。实际文件里常见的是第 1 行是标题
（「三亚报价单」合并单元格）、第 2 行才是表头。所以这里取**第一行有两个以上
非空单元格**的那一行当表头——标题行通常只有一个格子有字。
"""

from __future__ import annotations

import csv
import io
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services.docs.reader import DocError

# 能当「表」读的格式。CSV 必须在内：用户手上的表格十有八九是从系统导出的 csv，
# 只认 xlsx 等于让最常见的一种文件走「模型自己数」那条路——那正是最容易错的
SHEET_SUFFIXES = frozenset({".xlsx", ".xlsm", ".csv", ".tsv"})
DELIMITED_SUFFIXES = frozenset({".csv", ".tsv"})

# 一次最多读多少数据行。十万行的台账全读进内存既慢又没必要——
# 用户问「大于 5000 的有哪些」时，前两万行足够给出答案
MAX_DATA_ROWS = 20000

# 支持的筛选算子。写成白名单而不是 eval 表达式：模型给的字符串直接参与
# 比较逻辑，一旦允许任意表达式就等于把 ``eval`` 暴露给提示词
OPS = ("eq", "ne", "contains", "not_contains", "gt", "gte", "lt", "lte", "empty", "not_empty")

_OP_TEXT = {
    "eq": "等于",
    "ne": "不等于",
    "contains": "包含",
    "not_contains": "不包含",
    "gt": "大于",
    "gte": "大于等于",
    "lt": "小于",
    "lte": "小于等于",
    "empty": "为空",
    "not_empty": "不为空",
}


@dataclass
class SheetTable:
    """一张表：表头 + 数据行（不含表头）。"""

    sheet: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False

    @property
    def width(self) -> int:
        return len(self.headers)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _cell_value(value: Any) -> Any:
    """能直接写进单元格的类型才原样写。

    模型偶尔会把单元格写成列表或字典（``{"value": 3}``），openpyxl 遇到这种
    直接抛异常，整份表就白生成了。转成字符串至少表是对的，用户能自己改。
    """
    if value is None or isinstance(value, str | int | float | bool | datetime):
        return value
    return str(value)


def load_table(path: Path, sheet_name: str = "") -> SheetTable:
    """读整张表（最多 ``MAX_DATA_ROWS`` 行）。

    ``.xlsx`` 走 openpyxl，``.csv`` / ``.tsv`` 走标准库 ``csv``——不为了读逗号分隔
    的文本装 pandas，那是几十 MB 的依赖换一行 ``csv.reader``。
    """
    if path.suffix.lower() in DELIMITED_SUFFIXES:
        return _load_delimited(path)

    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise DocError("缺少 openpyxl，无法读取 .xlsx") from exc

    try:
        workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    except Exception as exc:
        raise DocError(f"打不开这个 .xlsx：{exc}") from exc

    try:
        if sheet_name:
            if sheet_name not in workbook.sheetnames:
                raise DocError(
                    f"没有工作表「{sheet_name}」，可选：{'、'.join(workbook.sheetnames)}"
                )
            sheet = workbook[sheet_name]
        else:
            sheet = workbook.worksheets[0]

        headers, rows, truncated = _collect(
            (list(raw) for raw in sheet.iter_rows(values_only=True)), sheet.title
        )
        return SheetTable(sheet=sheet.title, headers=headers, rows=rows, truncated=truncated)
    finally:
        workbook.close()


def _collect(source: Iterator[list[Any]], title: str) -> tuple[list[str], list[list[Any]], bool]:
    """从「一行行单元格」里挑出表头和数据行。

    xlsx 和 csv 共用这一段：两边唯一的区别是行从哪来，而「哪一行是表头、
    空行怎么办、超了多少行截断」这些判断必须完全一致，否则同一份数据
    存成两种格式会得到两种结果。
    """
    headers: list[str] | None = None
    rows: list[list[Any]] = []
    truncated = False
    for values in source:
        if headers is None:
            if sum(1 for value in values if not _is_blank(value)) >= 2:
                headers = ["" if value is None else str(value).strip() for value in values]
            continue
        if all(_is_blank(value) for value in values):
            continue
        if len(rows) >= MAX_DATA_ROWS:
            truncated = True
            break
        rows.append(values)

    if headers is None:
        raise DocError(f"「{title}」里没找到表头（需要至少两个非空单元格的一行）")
    # 去掉表头尾部的空列，否则每行输出一长串「 |  |  | 」
    while headers and not headers[-1]:
        headers.pop()
    return headers, rows, truncated


def _sniff_delimiter(sample: str) -> str:
    """猜分隔符。猜不出来就按逗号——那是 CSV 的本意，猜错也只是整行一列。"""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _load_delimited(path: Path) -> SheetTable:
    """读 .csv / .tsv。

    编码用和 ``read_document`` 同一套降级链（utf-8 → gb18030）：Excel 导出的
    中文 CSV 在 Windows 上默认是 GBK，直接按 utf-8 读会抛异常或者得到一片问号。
    """
    from app.services.docs.reader import _decode

    try:
        text = _decode(path.read_bytes())
    except OSError as exc:
        raise DocError(f"打不开这个文件：{exc}") from exc
    if not text.strip():
        raise DocError(f"「{path.name}」是空文件")

    if path.suffix.lower() == ".tsv":
        delimiter = "\t"
    else:
        delimiter = _sniff_delimiter("\n".join(text.splitlines()[:5]))

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    headers, rows, truncated = _collect((list(row) for row in reader), path.name)
    return SheetTable(sheet=path.stem, headers=headers, rows=rows, truncated=truncated)


def resolve_column(table: SheetTable, column: str) -> int:
    """把「列名 / 列字母 / 序号」统一成 0 基下标。"""
    from openpyxl.utils import column_index_from_string

    wanted = (column or "").strip()
    if not wanted:
        raise DocError("没有指定列")

    for index, header in enumerate(table.headers):
        if header.strip().lower() == wanted.lower():
            return index

    # 允许 A / B / AA 这种列字母。只在长度够短时尝试，避免把中文列名当字母解析
    if wanted.isascii() and wanted.isalpha() and len(wanted) <= 3:
        try:
            return column_index_from_string(wanted.upper()) - 1
        except ValueError:  # pragma: no cover - 已限定字母
            pass

    if wanted.isdigit():
        return int(wanted) - 1

    available = "、".join(header for header in table.headers if header) or "（无）"
    raise DocError(f"没有列「{column}」，可选：{available}")


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("￥", "").strip())
        except ValueError:
            return None
    return None


def _compare(left: Any, op: str, right: Any) -> bool:
    if op in {"empty", "not_empty"}:
        blank = _is_blank(left)
        return blank if op == "empty" else not blank

    if op in {"contains", "not_contains"}:
        hit = str(right).lower() in ("" if left is None else str(left)).lower()
        return hit if op == "contains" else not hit

    left_num, right_num = _as_number(left), _as_number(right)
    if op in {"gt", "gte", "lt", "lte"}:
        # 两边都是数字才按数值比，否则退回字符串比较（日期、编号常是文本）。
        # 两个分支分开写而不是合成一个变量：float 和 str 混在一个变量里，
        # mypy 没法判断比较是否合法，而运行时的 TypeError 要到用户那里才暴露
        if left_num is not None and right_num is not None:
            if op == "gt":
                return left_num > right_num
            if op == "gte":
                return left_num >= right_num
            if op == "lt":
                return left_num < right_num
            return left_num <= right_num

        left_text = "" if left is None else str(left)
        right_text = str(right)
        if op == "gt":
            return left_text > right_text
        if op == "gte":
            return left_text >= right_text
        if op == "lt":
            return left_text < right_text
        return left_text <= right_text

    if op == "ne":
        return str(left) != str(right)
    # eq：数字按数值比，避免 5000 与 "5000" 被判成不等
    if left_num is not None and right_num is not None:
        return left_num == right_num
    return ("" if left is None else str(left)) == str(right)


def apply_filters(table: SheetTable, rules: list[dict[str, Any]]) -> list[list[Any]]:
    """多条件同时满足（AND）。"""
    rows = table.rows
    for rule in rules:
        column = str(rule.get("column") or "")
        op = str(rule.get("op") or "eq").strip()
        if op not in OPS:
            raise DocError(f"不支持的筛选方式「{op}」，可用：{'、'.join(OPS)}")
        index = resolve_column(table, column)
        value = rule.get("value")
        rows = [row for row in rows if _compare(_cell(row, index), op, value)]
    return rows


def _cell(row: list[Any], index: int) -> Any:
    return row[index] if 0 <= index < len(row) else None


RowKey = Callable[[list[Any]], "tuple[int, float, str]"]


def _sorted_blanks_last(rows: list[list[Any]], key: RowKey, descending: bool) -> list[list[Any]]:
    """排序，但空值恒排最后。

    直接 ``sorted(reverse=True)`` 会把空值的 ``(1, 0.0, "")`` 翻到最前面——
    「没有数据的城市」于是看起来成了「花得最多的城市」。所以先把有值的排完，
    空值原样接在后面。
    """
    filled = [row for row in rows if key(row)[0] == 0]
    blanks = [row for row in rows if key(row)[0] != 0]
    return sorted(filled, key=key, reverse=descending) + blanks


def sort_rows(
    table: SheetTable, rows: list[list[Any]], by: str, descending: bool
) -> list[list[Any]]:
    """稳定排序；数字按数值，其它按字符串。空值永远排最后。"""
    if not by:
        return rows
    index = resolve_column(table, by)

    def key(row: list[Any]) -> tuple[int, float, str]:
        value = _cell(row, index)
        if _is_blank(value):
            return (1, 0.0, "")
        number = _as_number(value)
        if number is not None:
            return (0, number, "")
        return (0, 0.0, str(value))

    return _sorted_blanks_last(rows, key, descending)


def describe(rules: list[dict[str, Any]]) -> str:
    """把筛选条件说成人话，用在工具返回的第一行和确认卡片里。"""
    parts: list[str] = []
    for rule in rules:
        column = str(rule.get("column") or "")
        op = str(rule.get("op") or "eq")
        text = _OP_TEXT.get(op, op)
        value = rule.get("value")
        parts.append(f"{column} {text}" + ("" if op in {"empty", "not_empty"} else f" {value}"))
    return " 且 ".join(parts)


def render(table: SheetTable, rows: list[list[Any]], limit: int) -> str:
    """渲染成表格文本。列多时截断列，避免一行几百字符。"""
    width = min(table.width, 12)
    # 工作表名已经在标题行说过一次了，这里不再重复
    lines = [f"表头：{' | '.join(table.headers[:width])}"]
    for row in rows[:limit]:
        cells = ["" if value is None else str(value) for value in row[:width]]
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def export_copy(
    path: Path,
    table: SheetTable,
    rows: list[list[Any]],
    title: str = "筛选结果",
    save_as: Path | None = None,
) -> Path:
    """把结果另存成副本。原文件一个字节都不动。

    xlsx 是「复制文件 + 新增一页」；csv 没有「工作表」这个概念，只能另写一个
    ``原名-筛选结果.csv``——硬要往 csv 里塞第二张表，等于把它写成坏文件。

    ``save_as`` 是用户指定的输出路径：他说「存到桌面」时传它。不给就落在源文件旁边。
    """
    from app.services.docs.editor import copy_target

    if path.suffix.lower() in DELIMITED_SUFFIXES:
        return _export_delimited(path, table, rows, save_as)

    from openpyxl import load_workbook

    output = copy_target(path, save_as)
    try:
        shutil.copyfile(path, output)
        workbook = load_workbook(filename=str(output))
    except OSError as exc:
        raise DocError(f"复制文件失败：{exc}") from exc

    try:
        if title in workbook.sheetnames:
            del workbook[title]
        sheet = workbook.create_sheet(title)
        sheet.append(table.headers)
        for row in rows:
            sheet.append([None if _is_blank(value) else value for value in row])
        workbook.save(str(output))
    except Exception as exc:
        raise DocError(f"写入结果失败：{exc}") from exc
    finally:
        workbook.close()
    return output


def _export_delimited(
    path: Path, table: SheetTable, rows: list[list[Any]], save_as: Path | None = None
) -> Path:
    from app.services.docs.editor import free_path

    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    default = path.with_name(f"{path.stem}-筛选结果{path.suffix}")
    target = free_path(save_as or default)
    try:
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter)
            writer.writerow(table.headers)
            for row in rows:
                writer.writerow(["" if _is_blank(value) else value for value in row])
    except OSError as exc:
        raise DocError(f"写入结果失败：{exc}") from exc
    return target


# ---------- 生成新表 ----------


def _sheet_title(raw: str) -> str:
    """工作表名：Excel 不允许 ``[]:*?/\\`` 和超过 31 个字符。"""
    name = "".join(ch for ch in (raw or "Sheet1") if ch not in "[]:*?/\\").strip()
    return (name or "Sheet1")[:31]


def _display_width(value: Any) -> int:
    """按显示宽度算：中文占两格。按 ``len`` 算会让「机构名称」被挤成一列井号。"""
    text = "" if value is None else str(value)
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _autosize(sheet_obj: Any) -> None:
    for column in sheet_obj.iter_cols():
        widest = max((_display_width(cell.value) for cell in column), default=0)
        sheet_obj.column_dimensions[column[0].column_letter].width = min(42, max(8, widest + 2))


def write_table(path: Path, rows: list[list[Any]], sheet: str = "Sheet1") -> Path:
    """从零写一份表，第一行当表头（加粗、冻结）。返回真正落盘的路径。

    **为什么需要它**：``edit_document`` 只能改已有文件，而且 ``set_cell``
    要求工作表已经存在——「把这 24 行做成一份新表」在那边只能让用户先手工
    建个空工作簿再往里填。这是本产品最常见的一次交付，不该这么绕。

    同名文件已存在时加序号，绝不覆盖：用户上一版结果还在，重跑一次不该把它
    冲掉。落盘路径由返回值带回，调用方要把它念给用户听。
    """
    if not rows or not any(any(not _is_blank(value) for value in row) for row in rows):
        raise DocError("没有数据可写：rows 是空的")

    from app.services.docs.editor import free_path

    suffix = path.suffix.lower()
    if suffix in DELIMITED_SUFFIXES:
        return _write_delimited(path, rows, suffix)
    if suffix != ".xlsx":
        raise DocError(f"只能生成 .xlsx / .csv / .tsv（{suffix or '无扩展名'} 不支持）")

    from openpyxl import Workbook
    from openpyxl.styles import Font

    target = free_path(path)
    workbook = Workbook()
    sheet_obj = workbook.active
    sheet_obj.title = _sheet_title(sheet)
    for row in rows:
        sheet_obj.append([None if _is_blank(value) else _cell_value(value) for value in row])
    for cell in sheet_obj[1]:
        cell.font = Font(bold=True)
    sheet_obj.freeze_panes = "A2"
    _autosize(sheet_obj)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(str(target))
    except OSError as exc:
        raise DocError(f"写入失败：{exc}") from exc
    finally:
        workbook.close()
    return target


def _write_delimited(path: Path, rows: list[list[Any]], suffix: str) -> Path:
    from app.services.docs.editor import free_path

    delimiter = "\t" if suffix == ".tsv" else ","
    target = free_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter)
            for row in rows:
                writer.writerow(["" if _is_blank(value) else value for value in row])
    except OSError as exc:
        raise DocError(f"写入失败：{exc}") from exc
    return target


# ---------- 汇总统计 ----------

# 支持的汇总方式。count 数「非空单元格」，其余都只吃数字——
# 把「含机票」这种文字算进 sum 会得到一个看起来正常、其实没意义的数
AGG_OPS = ("sum", "avg", "count", "min", "max")

_AGG_TEXT = {"sum": "合计", "avg": "平均", "count": "数量", "min": "最小", "max": "最大"}

# 分组键为空时的占位。用中文括号而不是空串：表格里空着一格看不出
# 「这个分组是有值但为空」还是「渲染漏了」
BLANK_GROUP = "（空）"


@dataclass
class AggregateResult:
    """一次汇总的结果。``skipped`` 是非数字单元格的数量，用来提示数据质量。"""

    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    scanned: int = 0
    skipped: int = 0


def _tidy(number: float) -> Any:
    """整数值的浮点去掉 ``.0``。

    ``_as_number`` 一律返回 float，于是「总金额 19800.0」——看着像程序日志，
    而金额本来就是整数。保留两位小数只是为了「客单价 4350.5」这类真需要小数的。
    """
    rounded = round(number, 2)
    return int(rounded) if float(rounded).is_integer() else rounded


def _metric_value(values: list[Any], op: str) -> Any:
    present = [value for value in values if not _is_blank(value)]
    if op == "count":
        return len(present)

    numbers = [number for number in (_as_number(value) for value in present) if number is not None]
    if op in {"sum", "avg"}:
        if not numbers:
            return None
        total = sum(numbers)
        return _tidy(total if op == "sum" else total / len(numbers))
    if op in {"min", "max"}:
        if numbers:
            return _tidy(min(numbers) if op == "min" else max(numbers))
        # 整列都是文本（日期、编号）时退回字符串比较，比返回空更有用
        texts = [str(value) for value in present]
        if not texts:
            return None
        return min(texts) if op == "min" else max(texts)
    return None


def _metric_alias(metric: dict[str, Any]) -> str:
    alias = str(metric.get("alias") or "").strip()
    if alias:
        return alias
    column = str(metric.get("column") or "").strip()
    op = str(metric.get("op") or "sum").strip()
    return f"{column or '行数'}·{_AGG_TEXT.get(op, op)}"


def fill_down(rows: list[list[Any]], table: SheetTable, columns: list[str]) -> list[list[Any]]:
    """把指定列的空单元格按**上一行**补齐，用来还原合并单元格。

    Excel 的合并单元格落到数据里，只有首行有值，其余是空的。按这一列分组，
    整片行会一起掉进「（空）」——用户看到的是「按网点汇总出一堆空网点」，
    而原表在他眼里明明是分好的。所以给一个显式的补值开关，而不是猜：
    补哪些列是用户的语义，程序猜不出来。

    返回新的行列表，不改 ``table.rows``：这份数据可能还要拿去做别的统计。
    """
    if not columns:
        return rows

    indexes = [resolve_column(table, column) for column in columns]
    filled: list[list[Any]] = []
    last: dict[int, Any] = {}
    for row in rows:
        current = list(row)
        for index in indexes:
            value = _cell(current, index)
            if _is_blank(value):
                if index in last:
                    while len(current) <= index:
                        current.append(None)
                    current[index] = last[index]
                continue
            last[index] = value
        filled.append(current)
    return filled


def blank_group_count(rows: list[list[Any]], table: SheetTable, column: str) -> int:
    """分组列里有多少行是空的。用来提示「这些行会掉进（空）」。"""
    index = resolve_column(table, column)
    return sum(1 for row in rows if _is_blank(_cell(row, index)))


def aggregate(
    table: SheetTable,
    rows: list[list[Any]],
    metrics: list[dict[str, Any]],
    group_by: str = "",
) -> AggregateResult:
    """按 ``group_by`` 分组（留空则整表一组）对每个指标求值。

    ``rows`` 传进来而不是从 ``table.rows`` 取，是为了能「先筛选再汇总」——
    「上海客户一共花了多少」比「全表一共多少」常用得多。
    """
    if not metrics:
        raise DocError('没有给出要汇总的指标，例如 [{"column":"金额","op":"sum"}]')

    plans: list[tuple[int, str, str]] = []
    for metric in metrics:
        op = str(metric.get("op") or "sum").strip()
        if op not in AGG_OPS:
            raise DocError(f"不支持的汇总方式「{op}」，可用：{'、'.join(AGG_OPS)}")
        column = str(metric.get("column") or "").strip()
        if op != "count" and not column:
            raise DocError(f"「{op}」必须指定 column")
        position = resolve_column(table, column) if column else -1
        plans.append((position, op, _metric_alias(metric)))

    group_index = resolve_column(table, group_by) if group_by else -1

    buckets: dict[str, list[list[Any]]] = {}
    order: list[str] = []
    for row in rows:
        # 不能写成 ``_cell(...) or BLANK_GROUP``：分组键是 0 或 False 时会被
        # 当成空值。另外要去掉首尾空白——合并单元格导出的「营业部\n」带着换行，
        # 分组名里夹个换行，用户看到的就是「营业部⏎」而不是「营业部」
        if group_index < 0:
            key = BLANK_GROUP
        else:
            raw = _cell(row, group_index)
            key = ("" if raw is None else str(raw).strip()) or BLANK_GROUP
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    headers = ([table.headers[group_index] or "分组"] if group_index >= 0 else ["合计"]) + [
        alias for _position, _op, alias in plans
    ]

    result = AggregateResult(headers=headers, scanned=len(rows))
    for key in order:
        group_rows = buckets[key]
        line: list[Any] = [] if group_index < 0 else [key]
        for position, op, _alias in plans:
            values = (
                list(group_rows) if position < 0 else [_cell(row, position) for row in group_rows]
            )
            # 只有 sum / avg 会把文本格真的丢掉。min / max 对文本有字符串比较的
            # 兜底（见 _metric_value），再报「没参与计算」就成了假警报——按姓名
            # 取 max 会平白多出一句「24 个单元格不是数字」，看着像这列坏了
            if op in {"sum", "avg"}:
                result.skipped += sum(
                    1 for value in values if not _is_blank(value) and _as_number(value) is None
                )
            line.append(_metric_value(values, op))
        result.rows.append(line)

    return result


def render_aggregate(result: AggregateResult, limit: int) -> str:
    lines = [" | ".join(result.headers)]
    for row in result.rows[:limit]:
        # 空值渲染成「—」而不是空白：空白看不出是「没数据」还是「渲染漏了」
        lines.append(" | ".join("—" if value is None else str(value) for value in row))
    if len(result.rows) > limit:
        lines.append(f"…（共 {len(result.rows)} 个分组，这里显示前 {limit} 个）")
    return "\n".join(lines)


def sort_aggregate(result: AggregateResult, by: str, descending: bool) -> AggregateResult:
    """按某个指标列给分组结果排序——「哪个城市花得最多」全靠它。

    数字按数值比，空值永远排最后（否则「没有数据的城市」会排到最前面，
    看起来像是花得最少）。
    """
    if not by:
        return result

    wanted = by.strip()
    index: int | None = None
    for position, header in enumerate(result.headers):
        if header == wanted:
            index = position
            break
    if index is None:
        # 允许只写列名：指标别名默认是「金额·合计」，写「金额」也能命中
        for position, header in enumerate(result.headers):
            if header.split("·")[0] == wanted:
                index = position
                break
    if index is None:
        raise DocError(f"汇总结果里没有「{by}」，可选：{'、'.join(result.headers)}")

    def key(row: list[Any]) -> tuple[int, float, str]:
        value = _cell(row, index)
        if _is_blank(value):
            return (1, 0.0, "")
        number = _as_number(value)
        if number is not None:
            return (0, number, "")
        return (0, 0.0, str(value))

    return AggregateResult(
        headers=result.headers,
        rows=_sorted_blanks_last(result.rows, key, descending),
        scanned=result.scanned,
        skipped=result.skipped,
    )
