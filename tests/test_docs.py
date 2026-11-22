"""文档能力：读、改副本、整理。

这里的用例都直接操作真实文件（docx / xlsx 都是真格式），不用 mock——
文档处理的坑全在「真实文件长什么样」上：Word 会把占位符拆成多个 run、
Excel 的空单元格是 None、表格内容不在 paragraphs 里。mock 掉这些细节，
测试就变成了自说自话。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services.docs.editor import apply_edits, copy_target
from app.services.docs.organizer import apply_plan, build_plan, parse_categories
from app.services.docs.reader import DocError, digest, list_documents, read_document
from app.services.docs.sheet import (
    BLANK_GROUP,
    aggregate,
    apply_filters,
    blank_group_count,
    export_copy,
    fill_down,
    load_table,
    render_aggregate,
    sort_aggregate,
    sort_rows,
)

# ---------- 造样本 ----------


def _make_docx(path: Path, *, split_runs: bool = False) -> Path:
    from docx import Document

    document = Document()
    document.add_heading("云南五日游服务合同", level=1)
    document.add_paragraph("甲方：{{客户名}}")
    if split_runs:
        # 复现 Word 的真实行为：占位符被拆成多个 run
        paragraph = document.add_paragraph()
        paragraph.add_run("乙方：{{")
        paragraph.add_run("旅行社")
        paragraph.add_run("}}")
    else:
        document.add_paragraph("乙方：{{旅行社}}")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "人数"
    table.cell(0, 1).text = "金额"
    table.cell(1, 0).text = "2"
    table.cell(1, 1).text = "4800"
    document.save(str(path))
    return path


def _make_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价"
    sheet.append(["项目", "单价", "数量"])
    sheet.append(["机票", 1200, 2])
    second = workbook.create_sheet("客户")
    second.append(["姓名", "电话"])
    second.append(["张三", "13800000000"])
    workbook.save(str(path))
    return path


# ---------- 读取 ----------


def test_read_docx_keeps_table_content(tmp_path: Path):
    """表格必须在正文里出现——合同的金额几乎都在表格里。"""
    path = _make_docx(tmp_path / "合同.docx")
    content = read_document(path).to_text()

    assert "云南五日游服务合同" in content
    assert "甲方" in content
    assert "4800" in content, "表格内容被漏掉了"
    assert "[表格 2 行 × 2 列]" in content


def test_read_docx_marks_headings(tmp_path: Path):
    path = _make_docx(tmp_path / "合同.docx")
    doc = read_document(path)

    assert any(block.kind == "heading" for block in doc.blocks)
    assert doc.title == "云南五日游服务合同"


def test_read_document_pages_with_start_and_limit(tmp_path: Path):
    """分段读：第一次没读完时，第二次能接着读，且不重复。"""
    path = _make_docx(tmp_path / "合同.docx")
    first = read_document(path, start=0, limit=2)
    second = read_document(path, start=2, limit=2)

    assert first.has_more
    assert first.offset == 0
    assert second.offset == 2
    assert not {block.text for block in first.blocks} & {block.text for block in second.blocks}
    assert "start=2" in first.to_text()


def test_read_xlsx_lists_every_sheet(tmp_path: Path):
    path = _make_xlsx(tmp_path / "报价.xlsx")
    content = read_document(path).to_text()

    assert "工作表「报价」" in content
    assert "工作表「客户」" in content
    assert "机票" in content
    assert "13800000000" in content


def test_read_text_falls_back_to_gb18030(tmp_path: Path):
    """Windows 导出的 csv 常见是 GBK，按 UTF-8 硬读会得到一片问号。"""
    path = tmp_path / "名单.csv"
    path.write_bytes("姓名,城市\n张三,三亚\n".encode("gb18030"))

    content = read_document(path).to_text()
    assert "张三" in content
    assert "三亚" in content


def test_read_xlsx_shows_uncached_formula(tmp_path: Path):
    """没被 Excel 保存过的公式没有缓存值，不能显示成空白。

    脚本生成或保存中断的 xlsx，``data_only=True`` 读公式格得到 None。
    如果直接输出空字符串，模型会以为「小计这一列没数据」。
    """
    from openpyxl import Workbook

    path = tmp_path / "报价.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["单价", "数量", "小计"])
    sheet.append([100, 2, "=A2*B2"])
    workbook.save(str(path))

    content = read_document(path).to_text()
    assert "=A2*B2（未计算）" in content
    assert "未计算的公式" in content


def test_legacy_xls_tells_user_what_to_do(tmp_path: Path):
    path = tmp_path / "旧表.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0")

    with pytest.raises(DocError) as excinfo:
        read_document(path)
    assert "另存为" in str(excinfo.value)


def test_unsupported_suffix_says_what_is_supported(tmp_path: Path):
    path = tmp_path / "扫描件.pdf"
    path.write_bytes(b"%PDF-1.4")

    with pytest.raises(DocError) as excinfo:
        read_document(path)
    assert "不支持" in str(excinfo.value)
    assert ".docx" in str(excinfo.value)


# ---------- 编辑 ----------


def test_edit_writes_copy_and_keeps_original(tmp_path: Path):
    path = _make_docx(tmp_path / "合同.docx")
    original = path.read_bytes()

    outcome = apply_edits(path, [{"op": "replace", "find": "{{客户名}}", "replace": "张三"}])

    assert outcome.output.name == "合同-已编辑.docx"
    assert path.read_bytes() == original, "原文件被改动了"
    from docx import Document

    assert "张三" in "\n".join(p.text for p in Document(str(outcome.output)).paragraphs)


def test_edit_replaces_placeholder_split_across_runs(tmp_path: Path):
    """Word 会把 ``{{旅行社}}`` 拆成三个 run，逐 run 匹配是找不到的。"""
    path = _make_docx(tmp_path / "合同.docx", split_runs=True)
    outcome = apply_edits(path, [{"op": "replace", "find": "{{旅行社}}", "replace": "海风国旅"}])

    from docx import Document

    text = "\n".join(p.text for p in Document(str(outcome.output)).paragraphs)
    assert "乙方：海风国旅" in text
    assert "旅行社}}" not in text


def test_edit_does_not_overwrite_existing_copy(tmp_path: Path):
    path = _make_docx(tmp_path / "合同.docx")
    (tmp_path / "合同-已编辑.docx").write_bytes(b"old")

    assert copy_target(path).name == "合同-已编辑2.docx"


def test_edit_xlsx_cell_and_row(tmp_path: Path):
    path = _make_xlsx(tmp_path / "报价.xlsx")
    outcome = apply_edits(
        path,
        [
            {"op": "set_cell", "sheet": "报价", "cell": "C2", "value": 3},
            {"op": "append_row", "sheet": "客户", "values": ["李四", "13900000000"]},
        ],
    )

    from openpyxl import load_workbook

    workbook = load_workbook(str(outcome.output))
    assert workbook["报价"]["C2"].value == 3
    assert workbook["客户"].max_row == 3
    workbook.close()


def test_edit_without_any_match_leaves_no_file(tmp_path: Path):
    """一项都没生效时不该在用户目录里留个空副本。"""
    path = _make_docx(tmp_path / "合同.docx")

    with pytest.raises(DocError) as excinfo:
        apply_edits(path, [{"op": "replace", "find": "根本不存在的文字", "replace": "x"}])
    assert "没生效" in str(excinfo.value)
    assert list(tmp_path.glob("*已编辑*")) == []


def test_edit_rejects_pdf(tmp_path: Path):
    path = tmp_path / "行程.pdf"
    path.write_bytes(b"%PDF-1.4")

    with pytest.raises(DocError) as excinfo:
        apply_edits(path, [{"op": "replace", "find": "a", "replace": "b"}])
    assert "PDF" in str(excinfo.value)


# ---------- 整理 ----------


def _touch(root: Path, *names: str) -> None:
    for name in names:
        (root / name).write_text("x", encoding="utf-8")


def test_organize_plan_classifies_and_skips_unknown(tmp_path: Path):
    _touch(tmp_path, "华东合同.docx", "报价单-三亚.xlsx", "随手记.txt")
    rules = parse_categories(
        [
            {"name": "合同", "keywords": ["合同"]},
            {"name": "报价", "keywords": ["报价", "报价单"]},
        ]
    )

    plan = build_plan(tmp_path, rules)

    assert {move.src.name for move in plan.moves} == {"华东合同.docx", "报价单-三亚.xlsx"}
    assert ("随手记.txt", "没命中任何分类关键词") in plan.skipped
    assert "还没有动任何文件" in plan.to_text()
    # 计划阶段不落盘
    assert not (tmp_path / "合同").exists()


def test_organize_apply_moves_files(tmp_path: Path):
    _touch(tmp_path, "华东合同.docx")
    rules = parse_categories([{"name": "合同", "keywords": ["合同"]}])

    plan = build_plan(tmp_path, rules)
    done = apply_plan(plan)

    assert len(done) == 1
    assert (tmp_path / "合同" / "华东合同.docx").is_file()
    assert not (tmp_path / "华东合同.docx").exists()


def test_organize_rename_pattern(tmp_path: Path):
    _touch(tmp_path, "华东合同.docx")
    rules = parse_categories([{"name": "合同", "keywords": ["合同"]}])

    plan = build_plan(tmp_path, rules, rename_pattern="{category}_{title}")

    assert plan.moves[0].dst.name == "合同_华东合同.docx"


def test_organize_does_not_reprocess_category_folders(tmp_path: Path):
    """第二次整理不能把已经在「合同」里的文件再挪一层。"""
    folder = tmp_path / "合同"
    folder.mkdir()
    _touch(folder, "华东合同.docx")
    rules = parse_categories([{"name": "合同", "keywords": ["合同"]}])

    plan = build_plan(tmp_path, rules)

    assert plan.empty


def test_organize_rejects_category_without_keywords():
    with pytest.raises(DocError):
        parse_categories([{"name": "合同", "keywords": []}])


def test_organize_sanitizes_path_traversal(tmp_path: Path):
    """分类名里带 ../ 不能把文件写到目录外面。"""
    _touch(tmp_path, "华东合同.docx")
    rules = parse_categories([{"name": "../../逃逸", "keywords": ["合同"]}])

    plan = build_plan(tmp_path, rules)

    assert tmp_path in plan.moves[0].dst.parents


# ---------- 目录概览 ----------


def test_digest_and_list_documents(tmp_path: Path):
    _make_docx(tmp_path / "合同.docx")
    _make_xlsx(tmp_path / "报价.xlsx")
    (tmp_path / "忽略.pdf").write_bytes(b"%PDF-1.4")

    files = list_documents(tmp_path)
    assert {item.suffix for item in files} == {".docx", ".xlsx"}

    line = digest(tmp_path / "合同.docx").to_line()
    assert "合同.docx" in line
    assert "云南五日游服务合同" in line


# ---------- 筛选与排序 ----------


def _make_orders(path: Path) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    # 第一行是标题（只有一个格子有字），表头在第二行——真实文件常见
    sheet.append(["2026 年 11 月订单"])
    sheet.append(["客户", "城市", "金额", "备注"])
    for customer, city, amount, note in [
        ("张三", "上海", 5200, "含机票"),
        ("李四", "三亚", 1800, ""),
        ("王五", "上海", 9600, "含酒店"),
        ("赵六", "杭州", 300, "仅接送"),
        ("钱七", "上海", 5000, ""),
    ]:
        sheet.append([customer, city, amount, note])
    workbook.save(str(path))
    return path


def test_sheet_headers_skip_title_row(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    assert table.sheet == "订单"
    assert table.headers[:4] == ["客户", "城市", "金额", "备注"]
    assert len(table.rows) == 5


def test_sheet_filter_by_number(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    rows = apply_filters(table, [{"column": "金额", "op": "gt", "value": 5000}])

    assert [row[0] for row in rows] == ["张三", "王五"]


def test_sheet_filter_text_and_multiple_conditions(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    rows = apply_filters(
        table,
        [
            {"column": "城市", "op": "eq", "value": "上海"},
            {"column": "金额", "op": "gte", "value": 5200},
        ],
    )

    assert [row[0] for row in rows] == ["张三", "王五"]


def test_sheet_filter_numeric_string_compares_as_number(tmp_path: Path):
    """「5000」和 5000 必须算相等，否则用户按文本存的金额永远筛不出来。"""
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    rows = apply_filters(table, [{"column": "金额", "op": "eq", "value": "5000"}])

    assert [row[0] for row in rows] == ["钱七"]


def test_sheet_sort_descending_numbers(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    rows = sort_rows(table, table.rows, "金额", True)

    assert [row[0] for row in rows] == ["王五", "张三", "钱七", "李四", "赵六"]


def test_sheet_sort_by_column_letter(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    rows = sort_rows(table, table.rows, "C", False)

    assert rows[0][0] == "赵六"


def test_sheet_unknown_column_lists_headers(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    with pytest.raises(DocError) as excinfo:
        apply_filters(table, [{"column": "价格", "op": "gt", "value": 1}])
    assert "金额" in str(excinfo.value)


def test_sheet_unknown_op_rejected(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    with pytest.raises(DocError):
        apply_filters(table, [{"column": "金额", "op": "__import__", "value": 1}])


def test_sheet_export_adds_result_sheet_and_keeps_original(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    original = path.read_bytes()
    table = load_table(path)
    rows = apply_filters(table, [{"column": "城市", "op": "eq", "value": "上海"}])

    output = export_copy(path, table, rows)

    assert path.read_bytes() == original
    from openpyxl import load_workbook

    workbook = load_workbook(str(output))
    assert "筛选结果" in workbook.sheetnames
    assert "订单" in workbook.sheetnames
    result = workbook["筛选结果"]
    assert [cell.value for cell in result[1]][:4] == ["客户", "城市", "金额", "备注"]
    assert result.max_row == 4  # 表头 + 3 行
    workbook.close()


# ---------- 汇总统计 ----------


def _make_sparse(path: Path) -> Path:
    """金额有空格的样本，用来验证「空值不参与计算、也不被排到前面」。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["客户", "城市", "金额"])
    sheet.append(["甲", "上海", 100])
    sheet.append(["乙", "上海", None])
    sheet.append(["丙", "三亚", None])
    workbook.save(str(path))
    return path


def test_aggregate_group_sum(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(table, table.rows, [{"column": "金额", "op": "sum"}], "城市")

    assert result.headers == ["城市", "金额·合计"]
    # 分组按首次出现顺序：上海 → 三亚 → 杭州
    assert result.rows == [["上海", 19800], ["三亚", 1800], ["杭州", 300]]
    assert result.scanned == 5


def test_aggregate_avg_and_count(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(
        table,
        table.rows,
        [{"column": "金额", "op": "avg", "alias": "客单价"}, {"column": "客户", "op": "count"}],
        "城市",
    )

    assert result.headers == ["城市", "客单价", "客户·数量"]
    assert result.rows[0] == ["上海", 6600.0, 3]
    assert result.rows[1] == ["三亚", 1800.0, 1]


def test_aggregate_whole_table_without_group(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(
        table, table.rows, [{"column": "金额", "op": "sum"}, {"op": "count", "alias": "单数"}]
    )

    assert result.headers == ["合计", "金额·合计", "单数"]
    assert result.rows == [[21900, 5]]


def test_aggregate_count_without_column_counts_rows(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(table, table.rows, [{"op": "count"}], "城市")

    assert result.headers == ["城市", "行数·数量"]
    assert [row[1] for row in result.rows] == [3, 1, 1]


def test_aggregate_after_filter(tmp_path: Path):
    """先筛后汇：「上海客户一共花了多少」比「全表多少」常用得多。"""
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)
    rows = apply_filters(table, [{"column": "城市", "op": "eq", "value": "上海"}])

    result = aggregate(table, rows, [{"column": "金额", "op": "sum"}])

    assert result.rows == [[19800]]
    assert result.scanned == 3


def test_aggregate_sort_metric_desc(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(table, table.rows, [{"column": "金额", "op": "sum"}], "城市")
    ordered = sort_aggregate(result, "金额", descending=True)

    assert [row[0] for row in ordered.rows] == ["上海", "三亚", "杭州"]
    # 只写「金额」也能命中别名「金额·合计」
    assert ordered.headers == result.headers


def test_aggregate_sort_unknown_metric_lists_headers(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)
    result = aggregate(table, table.rows, [{"column": "金额", "op": "sum"}], "城市")

    with pytest.raises(DocError) as excinfo:
        sort_aggregate(result, "利润", descending=True)
    assert "金额·合计" in str(excinfo.value)


def test_aggregate_blank_metric_stays_last_when_descending(tmp_path: Path):
    path = _make_sparse(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(table, table.rows, [{"column": "金额", "op": "sum"}], "城市")
    ordered = sort_aggregate(result, "金额·合计", descending=True)

    # 三亚整组金额为空，求和无意义 → None，必须排最后而不是被 reverse 顶到第一
    assert ordered.rows[0] == ["上海", 100]
    assert ordered.rows[-1] == ["三亚", None]


def test_aggregate_skips_text_cells(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(table, table.rows, [{"column": "备注", "op": "sum"}])

    assert result.rows == [[None]]
    # 「含机票」「含酒店」「仅接送」三个文本格没参与计算，要能报出来
    assert result.skipped == 3


def test_aggregate_min_max_falls_back_to_text(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    result = aggregate(
        table,
        table.rows,
        [{"column": "金额", "op": "min"}, {"column": "金额", "op": "max"}],
        "城市",
    )

    assert result.rows[0] == ["上海", 5000, 9600]


def _make_merged_orgs(path: Path) -> Path:
    """合并单元格的网点表：机构名称只有每组首行有值，其余为空。"""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "业绩"
    sheet.append(["机构名称", "岗位", "姓名", "新增户数"])
    for org, people in [
        ("营业部", [("行长", "宋向明"), ("客户经理", "薛超")]),
        ("柳林支行", [("行长", "张志伟"), ("客户经理", "李娜")]),
    ]:
        for index, (post, name) in enumerate(people):
            # 首行写机构名，其余留空——合并单元格落成数据就是这样
            sheet.append([org if index == 0 else None, post, name, 1])
    workbook.save(str(path))
    return path


def test_fill_down_restores_merged_cells(tmp_path: Path):
    path = _make_merged_orgs(tmp_path / "业绩.xlsx")
    table = load_table(path)

    filled = fill_down(table.rows, table, ["机构名称"])

    assert [row[0] for row in filled] == ["营业部", "营业部", "柳林支行", "柳林支行"]
    # 原表不能被改：这份数据可能还要拿去做别的统计
    assert table.rows[1][0] is None


def test_fill_down_only_touches_listed_columns(tmp_path: Path):
    path = _make_merged_orgs(tmp_path / "业绩.xlsx")
    table = load_table(path)

    filled = fill_down(table.rows, table, ["岗位"])

    assert [row[0] for row in filled] == [row[0] for row in table.rows]
    assert [row[1] for row in filled] == ["行长", "客户经理", "行长", "客户经理"]


def test_aggregate_without_fill_down_groups_merged_rows_as_blank(tmp_path: Path):
    """不补值时的坏结果要能复现：整片行掉进「（空）」。"""
    path = _make_merged_orgs(tmp_path / "业绩.xlsx")
    table = load_table(path)

    result = aggregate(table, table.rows, [{"column": "新增户数", "op": "sum"}], "机构名称")

    assert ["营业部", 1] in result.rows
    # 两个网点各有一行空机构名，它们被并成同一个「（空）」——正是用户看到的坏结果
    assert [BLANK_GROUP, 2] in result.rows


def test_aggregate_after_fill_down_groups_by_org(tmp_path: Path):
    path = _make_merged_orgs(tmp_path / "业绩.xlsx")
    table = load_table(path)
    rows = fill_down(table.rows, table, ["机构名称"])

    result = aggregate(table, rows, [{"column": "新增户数", "op": "sum"}], "机构名称")

    assert result.rows == [["营业部", 2], ["柳林支行", 2]]


def test_blank_group_count_reports_merged_rows(tmp_path: Path):
    path = _make_merged_orgs(tmp_path / "业绩.xlsx")
    table = load_table(path)

    assert blank_group_count(table.rows, table, "机构名称") == 2


def test_group_key_is_stripped_of_newlines(tmp_path: Path):
    """合并单元格导出的「营业部\\n」带着换行，分组名里不能留着它。"""
    from openpyxl import Workbook

    path = tmp_path / "带换行.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["机构名称", "金额"])
    sheet.append(["营业部\n", 10])
    workbook.save(str(path))
    table = load_table(path)

    result = aggregate(table, table.rows, [{"column": "金额", "op": "sum"}], "机构名称")

    assert result.rows == [["营业部", 10]]


def test_aggregate_requires_metric(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    with pytest.raises(DocError) as excinfo:
        aggregate(table, table.rows, [], "城市")
    assert "指标" in str(excinfo.value)


def test_aggregate_rejects_unknown_op(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    with pytest.raises(DocError) as excinfo:
        aggregate(table, table.rows, [{"column": "金额", "op": "median"}])
    assert "sum" in str(excinfo.value)


def test_aggregate_non_count_needs_column(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)

    with pytest.raises(DocError):
        aggregate(table, table.rows, [{"op": "sum"}])


def test_aggregate_render_mentions_truncation(tmp_path: Path):
    path = _make_orders(tmp_path / "订单.xlsx")
    table = load_table(path)
    result = aggregate(table, table.rows, [{"column": "金额", "op": "sum"}], "城市")

    text = render_aggregate(result, 2)

    assert text.splitlines()[0] == "城市 | 金额·合计"
    assert "共 3 个分组" in text


# ---------- CSV / TSV 也当表读 ----------


def test_csv_is_read_as_table(tmp_path: Path):
    """用户手上的表格十有八九是从系统导出的 csv，只认 xlsx 会把它逼到「模型自己数」。"""
    path = tmp_path / "订单.csv"
    path.write_text("客户,城市,金额\n张三,上海,5200\n李四,三亚,1800\n", encoding="utf-8")

    table = load_table(path)

    assert table.sheet == "订单"
    assert table.headers == ["客户", "城市", "金额"]
    assert table.rows[0] == ["张三", "上海", "5200"]


def test_csv_gbk_encoding(tmp_path: Path):
    """Excel 在 Windows 上导出的中文 csv 默认是 GBK，按 utf-8 读会整片乱码。"""
    path = tmp_path / "订单.csv"
    path.write_bytes("客户,城市,金额\n张三,上海,5200\n".encode("gb18030"))

    table = load_table(path)

    assert table.headers == ["客户", "城市", "金额"]
    assert table.rows[0][0] == "张三"


def test_tsv_uses_tab_delimiter(tmp_path: Path):
    path = tmp_path / "订单.tsv"
    path.write_text("客户\t城市\t金额\n张三\t上海\t5200\n", encoding="utf-8")

    table = load_table(path)

    assert table.headers == ["客户", "城市", "金额"]


def test_csv_with_semicolon_delimiter(tmp_path: Path):
    """欧洲区导出的 csv 用分号。猜不出来就整行一列，后面所有筛选都会失配。"""
    path = tmp_path / "订单.csv"
    path.write_text("客户;城市;金额\n张三;上海;5200\n李四;三亚;1800\n", encoding="utf-8")

    table = load_table(path)

    assert table.headers == ["客户", "城市", "金额"]


def test_csv_header_detection_skips_title_row(tmp_path: Path):
    path = tmp_path / "订单.csv"
    path.write_text("2026 年 11 月订单\n客户,城市,金额\n张三,上海,5200\n", encoding="utf-8")

    table = load_table(path)

    assert table.headers == ["客户", "城市", "金额"]
    assert len(table.rows) == 1


def test_csv_filter_and_aggregate(tmp_path: Path):
    path = tmp_path / "订单.csv"
    path.write_text(
        "客户,城市,金额\n张三,上海,5200\n李四,三亚,1800\n王五,上海,9600\n", encoding="utf-8"
    )
    table = load_table(path)

    rows = apply_filters(table, [{"column": "城市", "op": "eq", "value": "上海"}])
    result = aggregate(table, rows, [{"column": "金额", "op": "sum"}])

    # csv 读出来是字符串，_as_number 要能把 "5200" 当数字加
    assert result.rows == [[14800]]


def test_csv_export_writes_new_file_not_broken_original(tmp_path: Path):
    """csv 没有工作表，往它里面塞第二页只会写出一个坏文件。"""
    path = tmp_path / "订单.csv"
    path.write_text("客户,城市,金额\n张三,上海,5200\n李四,三亚,1800\n", encoding="utf-8")
    original = path.read_bytes()
    table = load_table(path)
    rows = apply_filters(table, [{"column": "城市", "op": "eq", "value": "上海"}])

    output = export_copy(path, table, rows)

    assert path.read_bytes() == original
    assert output.name == "订单-筛选结果.csv"
    text = output.read_text(encoding="utf-8-sig")
    assert "张三" in text and "李四" not in text


# ---------- 生成新表 / 输出位置 ----------


def test_write_table_creates_readable_xlsx(tmp_path: Path):
    """从零写一份表：表头加粗、冻结首行、数字仍是数字（能求和）。"""
    from openpyxl import load_workbook

    from app.services.docs.sheet import write_table

    target = tmp_path / "行长汇总.xlsx"
    rows = [
        ["机构", "行长", "新增户数"],
        ["营业部", "宋向明", 8],
        ["柳林支行", "张志伟", 3],
    ]

    output = write_table(target, rows, "网点")

    assert output == target
    workbook = load_workbook(str(output))
    sheet = workbook["网点"]
    assert [c.value for c in sheet[1]] == ["机构", "行长", "新增户数"]
    assert sheet["C2"].value == 8
    assert isinstance(sheet["C2"].value, int)  # 不能变成字符串，否则 Excel 求不了和
    assert sheet["A1"].font.bold is True
    assert sheet.freeze_panes == "A2"
    workbook.close()


def test_write_table_never_overwrites(tmp_path: Path):
    """同名文件已存在就加序号：上一版结果不能被一次重跑冲掉。"""
    from app.services.docs.sheet import write_table

    first = write_table(tmp_path / "汇总.xlsx", [["a"], [1]])
    second = write_table(tmp_path / "汇总.xlsx", [["a"], [2]])

    assert first.name == "汇总.xlsx"
    assert second.name == "汇总2.xlsx"
    assert first.read_bytes() != second.read_bytes()


def test_write_table_rejects_empty_rows(tmp_path: Path):
    from app.services.docs.sheet import write_table

    with pytest.raises(DocError):
        write_table(tmp_path / "空.xlsx", [])
    with pytest.raises(DocError):
        write_table(tmp_path / "空.xlsx", [["", "  "]])


def test_write_table_sanitizes_nested_values(tmp_path: Path):
    """模型偶尔把单元格写成 dict/list，openpyxl 会直接抛——整份表就白生成了。"""
    from app.services.docs.sheet import write_table

    output = write_table(tmp_path / "怪值.xlsx", [["列"], [{"value": 3}]])

    assert output.exists()


def test_write_table_csv_is_excel_friendly(tmp_path: Path):
    """csv 要带 BOM，否则 Excel 双击打开是乱码。"""
    from app.services.docs.sheet import write_table

    output = write_table(tmp_path / "汇总.csv", [["机构", "户数"], ["营业部", 8]])

    raw = output.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "营业部" in raw.decode("utf-8-sig")


def test_export_copy_honours_save_as(tmp_path: Path):
    """用户说「存到桌面」：结果就得落在他给的目录里，而不是源文件旁边。"""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    path = _make_xlsx(tmp_path / "报价.xlsx")
    table = load_table(path)

    output = export_copy(path, table, table.rows, save_as=desktop / "汇总.xlsx")

    assert output == desktop / "汇总.xlsx"
    assert output.is_file()
    assert not (tmp_path / "报价-筛选结果.xlsx").exists()


def test_apply_edits_honours_save_as(tmp_path: Path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    path = _make_xlsx(tmp_path / "报价.xlsx")

    outcome = apply_edits(
        path,
        [{"op": "set_cell", "sheet": "报价", "cell": "B2", "value": 1300}],
        save_as=desktop / "改好的.xlsx",
    )

    assert outcome.output == desktop / "改好的.xlsx"
    assert outcome.output.is_file()


def test_save_target_accepts_dir_file_and_bare_name(tmp_path: Path):
    """模型给的 save_to 可能是目录、完整文件名、或只有个名字，三种都要收。"""
    from app.agent.tools.docs import _desktop, _save_target, _with_suffix

    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    assert _save_target(str(desktop), "汇总", ".xlsx") == desktop / "汇总.xlsx"
    assert _save_target(str(desktop) + os.sep, "汇总", ".xlsx") == desktop / "汇总.xlsx"
    assert _save_target(str(desktop / "我的表.xlsx"), "汇总", ".xlsx") == desktop / "我的表.xlsx"
    # 只给个名字时落到桌面：落到程序目录等于用户永远找不到
    bare = _save_target("我的表", "汇总", ".xlsx")
    assert bare is not None and bare.name == "我的表.xlsx" and bare.parent == _desktop()
    assert _save_target("", "汇总", ".xlsx") is None
    # 名字里的扩展名跟源文件对不上时要改回来，否则会交出「内容其实是 xlsx 的 csv」
    assert _with_suffix(desktop / "汇总.csv", ".xlsx") == desktop / "汇总.xlsx"


async def test_write_sheet_tool_writes_and_opens(tmp_path: Path, monkeypatch):
    """工具层跑通：落到指定目录、回报路径、顺手用默认程序打开。

    这一步是用户那次抱怨的正面用例——「整理好的新表」必须出现在他说的地方，
    并且已经打开，而不是留在 data/uploads 里让他自己去找。
    """
    from app.agent.permissions import (
        LocalDecisionChannel,
        PermissionBroker,
        current_broker,
    )
    from app.agent.tools import docs as docs_tool
    from app.core.security import register_root

    register_root(tmp_path)  # 假装这就是他的桌面
    opened: list[str] = []
    monkeypatch.setattr(docs_tool, "launch_default", lambda p: opened.append(str(p)))

    async def notify(request) -> None:
        raise AssertionError(f"不该再弹确认：{request.action}")

    broker = PermissionBroker(LocalDecisionChannel(), notify)
    broker._remembered.add(f"write_sheet:{tmp_path}")
    token = current_broker.set(broker)
    try:
        result = await docs_tool.write_sheet.ainvoke(
            {
                "path": str(tmp_path / "行长汇总.xlsx"),
                "rows": [["机构", "行长"], ["营业部", "宋向明"]],
                "sheet": "网点",
            }
        )
    finally:
        current_broker.reset(token)

    target = tmp_path / "行长汇总.xlsx"
    assert target.is_file()
    assert str(target) in result and "已生成" in result
    assert opened == [str(target)]
