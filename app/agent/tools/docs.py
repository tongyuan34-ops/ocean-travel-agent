"""docs 组工具：读文档、生成副本式编辑、批量整理。

**为什么单独成组，而不是塞进 system 组**

system 组做的是「原样读写一个文件」，模型拿到的是字节和行；docs 组做的是
「理解文档结构」——大纲、表格、章节、占位符。两者的失败方式不一样：读文件失败
通常是路径不对，读文档失败通常是格式不支持或文件损坏。分成两组，提示词里才能
分别写「什么时候用哪个」，模型也不会拿 ``read_file`` 去读 .docx 然后拿到乱码。

**为什么读操作也要过授权**

``read_document`` 会解析整份文档并回显内容，敏感度比 ``read_file`` 更高（合同、
客户名单）。所以它走和 ``read_file`` 同一套目录授权：没授权的目录先问一次，
问过之后不再问。

**为什么编辑一定要问**

``edit_document`` 写的是新文件，但它的输入是用户的原始资料，一旦匹配错了内容，
用户可能拿副本去覆盖原件。所以它按写操作处理：确认卡片里列出「要改哪几处」，
用户看清了再执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.permissions import PermissionDenied
from app.agent.tools.system import (
    PathInput,
    _ask_grant,
    _confirm_with_grant,
    _failed,
    _pending_grant,
    _roots,
    launch_default,
)
from app.core.context import truncate_tool_output
from app.core.security import PathNotAllowed, ensure_allowed
from app.services.docs.editor import apply_edits
from app.services.docs.organizer import apply_plan, build_plan, parse_categories
from app.services.docs.reader import DocError, digest, list_documents
from app.services.docs.reader import read_document as read_doc_text
from app.services.docs.sheet import (
    BLANK_GROUP,
    DELIMITED_SUFFIXES,
    MAX_DATA_ROWS,
    SHEET_SUFFIXES,
    aggregate,
    apply_filters,
    blank_group_count,
    describe,
    export_copy,
    load_table,
    render,
    render_aggregate,
    sort_aggregate,
    sort_rows,
    write_table,
)

# 工具的参数也叫 fill_down，直接 import 会被同名参数遮住
from app.services.docs.sheet import fill_down as fill_down_rows

# 一次回给模型的块数上限。再多就是让模型读长文，而不是让它知道结构
MAX_READ_LIMIT = 200
# 概览里最多列多少份
MAX_DIGEST = 40


class ReadDocInput(BaseModel):
    path: str = Field(description="文档路径（.docx/.xlsx/.md/.txt/.csv），必须在白名单内")
    start: int = Field(default=0, description="从第几块开始读，用于接着上次没读完的部分")
    limit: int = Field(default=60, description="这次读多少块，1..200")


class EditDocInput(BaseModel):
    path: str = Field(description="要编辑的文档路径（.docx 或 .xlsx）")
    operations: list[dict[str, Any]] = Field(
        description=(
            "编辑操作列表。docx 支持 "
            '{"op":"replace","find":"{{客户名}}","replace":"张三"} 和 '
            '{"op":"append","text":"补充条款","style":"Heading 1"}；'
            'xlsx 支持 {"op":"set_cell","sheet":"Sheet1","cell":"B3","value":"值"} 和 '
            '{"op":"append_row","sheet":"Sheet1","values":["a","b"]}'
        )
    )
    save_to: str = Field(
        default="",
        description=(
            "副本存到哪：目录（如 C:\\Users\\你\\Desktop）或完整文件名"
            "（如 C:\\Users\\你\\Desktop\\行长汇总.xlsx）。留空则存在原文件旁边。"
            "用户说「存到桌面 / 放到桌面 / 另存一份给我」时**必须**填这里"
        ),
    )


class WriteSheetInput(BaseModel):
    path: str = Field(
        description=(
            "要生成的文件完整路径，如 C:\\Users\\你\\Desktop\\行长汇总.xlsx。"
            "支持 .xlsx / .csv / .tsv；用户只说「新表」时默认 .xlsx 并放到桌面"
        )
    )
    rows: list[list[Any]] = Field(
        description=(
            '二维数据，**第一行是表头**。数字写数字（5200），不要写成字符串（"5200"），'
            "否则 Excel 里没法求和"
        )
    )
    sheet: str = Field(default="Sheet1", description="工作表名（.xlsx 用）")
    open_after: bool = Field(
        default=True,
        description="生成后用系统默认程序打开。用户说「做好给我看 / 打开」时保持 true",
    )


class OrganizeInput(BaseModel):
    path: str = Field(description="要整理的目录")
    categories: list[dict[str, Any]] = Field(
        description=(
            '分类规则，如 [{"name":"合同","keywords":["合同","协议"]},'
            ' {"name":"报价单","keywords":["报价","报价单"]}]'
        )
    )
    rename_pattern: str = Field(
        default="",
        description=(
            "重命名模板，留空表示不改名。可用占位符：{date} 修改日期、{title} 原名、"
            "{category} 分类名、{index} 序号。例：{date}_{title}"
        ),
    )
    apply: bool = Field(
        default=False,
        description="false 只出计划（默认）；用户确认后再用 true 真正移动文件",
    )


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _desktop() -> Path:
    """桌面。找不到就退回主目录——OneDrive 重定向过的机器桌面不在 ``~/Desktop``。"""
    home = Path.home()
    for candidate in (home / "Desktop", home / "OneDrive" / "Desktop"):
        if candidate.is_dir():
            return candidate
    return home


def _absolute(raw: str) -> Path:
    """模型给的路径可能只是「汇总.xlsx」。这种落到**桌面**，不要落到程序目录。"""
    text = (raw or "").strip().strip('"').strip("'")
    candidate = Path(text).expanduser()
    if candidate.parent == Path("."):
        return _desktop() / candidate.name
    return candidate


def _save_target(raw: str, stem: str, suffix: str) -> Path | None:
    """把模型给的 ``save_to`` 解析成完整输出路径；留空返回 None（用默认位置）。

    三种写法都收：目录（``…\\Desktop``）、完整文件名（``…\\Desktop\\汇总.xlsx``）、
    只给个名字（``汇总.xlsx``）。用户嘴里的「桌面」到了模型那里可能变成任意一种，
    为这点差异报错没有意义——落到桌面才是他要的结果。
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return None
    candidate = Path(text).expanduser()
    if candidate.is_dir() or text.endswith(("/", "\\")):
        return (candidate / f"{stem}{suffix}").resolve()
    target = _absolute(text)
    if target.suffix:
        return target.resolve()
    return target.with_suffix(suffix).resolve()


def _with_suffix(target: Path | None, suffix: str) -> Path | None:
    """扩展名跟着源文件走。

    模型可能把 xlsx 的结果命名成 ``汇总.csv``——那会得到一个「内容其实是
    xlsx 二进制」的 csv，Excel 打不开。宁可覆盖它给的名字，也不要交出一个坏文件。
    """
    if target is None or target.suffix.lower() == suffix:
        return target
    return target.with_suffix(suffix)


@tool(args_schema=ReadDocInput)
async def read_document(path: str, start: int = 0, limit: int = 60) -> str:
    """读取 Word / Excel / 纯文本文档的结构化内容（大纲、表格、正文块）。

    默认只返回前 60 块，末尾会提示还剩多少块、用 start 继续读。
    读合同、报价单、客户名单这类文档时先调这个，不要用 read_file 硬读二进制。
    """
    try:
        await _ask_grant(path)
        target = ensure_allowed(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    if not target.is_file():
        return _failed(f"不是文件：{target}", "DOC_READ_FAILED")

    try:
        content = read_doc_text(
            target, start=max(0, start), limit=min(MAX_READ_LIMIT, max(1, limit))
        )
    except DocError as exc:
        return _failed(str(exc), "DOC_READ_FAILED")

    return truncate_tool_output(content.to_text(), limit=6000)


@tool(args_schema=PathInput)
async def digest_documents(path: str) -> str:
    """列出目录里的文档并给出一行摘要（格式、块数、标题、开头一句）。

    用来决定「先读哪一份」，不要一上来就逐个 read_document——一个文件夹里
    二十份文档全读一遍既慢又超上下文。
    """
    try:
        await _ask_grant(path)
        target = ensure_allowed(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    if not target.is_dir():
        return _failed(f"不是目录：{target}", "DOC_READ_FAILED")

    files = list_documents(target, limit=MAX_DIGEST)
    if not files:
        return f"{target} 里没有可读的文档（支持 docx / xlsx / md / txt / csv）。"

    lines = [f"{target} 共 {len(files)} 份文档（数据来源：本地文件系统，按修改时间倒序）："]
    failed = 0
    for item in files:
        try:
            lines.append(digest(item).to_line())
        except DocError as exc:
            failed += 1
            lines.append(f"- {item.name}：读不出来（{exc}）")
    if failed:
        lines.append(f"其中 {failed} 份解析失败，可能是加密或损坏的文件。")
    return truncate_tool_output("\n".join(lines), limit=4000)


@tool(args_schema=EditDocInput)
async def edit_document(path: str, operations: list[dict[str, Any]], save_to: str = "") -> str:
    """编辑 Word / Excel 文档，**生成副本**（原名-已编辑），原文件永不改动。

    docx 支持按文字替换（含跨 run 的占位符）和追加段落；xlsx 支持写单元格和
    追加整行。执行前会请用户确认，确认卡片里列出要改的内容。

    用户说「存到桌面 / 另存一份给我」时传 ``save_to``（目录或完整文件名），
    副本就落在他说的地方；不传则留在原文件旁边。
    """
    try:
        # 先看是不是硬拒绝（系统目录），再解析——不要先弹框再拒绝
        _pending_grant(path)
        target = _resolve(path)
    except (PathNotAllowed, OSError, RuntimeError) as exc:
        message = getattr(exc, "message", str(exc))
        return _failed(message, getattr(exc, "code", "DOC_EDIT_FAILED"))

    if not target.is_file():
        return _failed(f"不是文件：{target}", "DOC_EDIT_FAILED")

    summary = "；".join(
        str(item.get("op") or "?") for item in operations[:5] if isinstance(item, dict)
    )
    save_as = _with_suffix(
        _save_target(save_to, f"{target.stem}-已编辑", target.suffix.lower()), target.suffix.lower()
    )
    where = f"，副本存到 {save_as}" if save_as is not None else ""
    try:
        await _confirm_with_grant(
            path,
            action="edit_document",
            detail={
                "path": str(target),
                "preview": (
                    f"将基于《{target.name}》生成一份副本并修改：{summary or '（未提供操作）'}"
                    f"（共 {len(operations)} 项）{where}。原文件不会被改动。"
                ),
            },
            remember_key=f"edit_document:{target}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        outcome = apply_edits(target, operations, save_as)
    except DocError as exc:
        return _failed(str(exc), "DOC_EDIT_FAILED")

    return truncate_tool_output(outcome.to_text(), limit=2000)


@tool(args_schema=OrganizeInput)
async def organize_documents(
    path: str,
    categories: list[dict[str, Any]],
    rename_pattern: str = "",
    apply: bool = False,
) -> str:
    """按关键词把目录里的文档分到子文件夹（可选重命名）。默认只出计划，不动文件。

    第一次调用用 apply=false 把计划给用户看，他确认后再用 apply=true 执行。
    只做移动和重命名，不删除、不覆盖；同名目标会自动加序号。
    """
    try:
        await _ask_grant(path)
        root = ensure_allowed(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    if not root.is_dir():
        return _failed(f"不是目录：{root}", "DOC_ORGANIZE_FAILED")

    try:
        rules = parse_categories(categories)
        plan = build_plan(root, rules, rename_pattern=rename_pattern)
    except DocError as exc:
        return _failed(str(exc), "DOC_ORGANIZE_FAILED")

    if not apply:
        return truncate_tool_output(plan.to_text(), limit=4000)

    if plan.empty:
        return truncate_tool_output(plan.to_text(), limit=2000)

    try:
        await _confirm_with_grant(
            path,
            action="organize_documents",
            detail={
                "path": str(root),
                "preview": f"将移动 {len(plan.moves)} 个文件到子文件夹（不删除、不覆盖）："
                + "；".join(f"{move.src.name}→{move.category}" for move in plan.moves[:5]),
            },
            remember_key=f"organize_documents:{root}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        done = apply_plan(plan)
    except DocError as exc:
        return _failed(str(exc), "DOC_ORGANIZE_FAILED")

    return truncate_tool_output(
        f"已移动 {len(done)} 个文件。\n" + plan.to_text(applied=True), limit=4000
    )


class QuerySheetInput(BaseModel):
    path: str = Field(description="表格文件路径（.xlsx / .xlsm / .csv / .tsv）")
    sheet: str = Field(default="", description="工作表名，留空表示第一个工作表")
    filters: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "筛选条件（多个条件同时满足）。每条形如 "
            '{"column":"金额","op":"gt","value":5000}。'
            "op 可选：eq 等于、ne 不等于、contains 包含、not_contains 不包含、"
            "gt 大于、gte 大于等于、lt 小于、lte 小于等于、empty 为空、not_empty 不为空"
        ),
    )
    sort_by: str = Field(default="", description="按哪一列排序（列名或列字母），留空不排序")
    descending: bool = Field(default=False, description="true 为降序（从大到小）")
    limit: int = Field(default=50, description="最多返回多少行，1..500")
    save_copy: bool = Field(
        default=False,
        description="true 时把结果另存为副本的新工作表「筛选结果」；默认只返回结果不改文件",
    )
    save_to: str = Field(
        default="",
        description=(
            "配合 save_copy 用：副本存到哪（目录如 C:\\Users\\你\\Desktop，或完整文件名）。"
            "留空则存在原文件旁边。用户说「存到桌面」时填这里"
        ),
    )


class AggregateInput(BaseModel):
    path: str = Field(description="表格文件路径（.xlsx / .xlsm / .csv / .tsv）")
    sheet: str = Field(default="", description="工作表名，留空表示第一个工作表")
    group_by: str = Field(default="", description="按哪一列分组（列名或列字母）；留空表示整表汇总")
    fill_down: list[str] = Field(
        default_factory=list,
        description=(
            "分组列是合并单元格时用：把这些列的空单元格按上一行补齐，再分组。"
            "例如「机构名称」只有每个网点首行有值（合并单元格），按它分组前写 "
            'fill_down=["机构名称"]，否则其余行会全掉进「（空）」。'
            "不确定时先不填——返回结果里会告诉你空分组占了多少行"
        ),
    )
    metrics: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            '要算的指标，如 [{"column":"金额","op":"sum","alias":"总金额"},'
            ' {"column":"订单号","op":"count","alias":"单数"}]。'
            "op 可选：sum 求和、avg 平均、count 计数、min 最小、max 最大"
        ),
    )
    filters: list[dict[str, Any]] = Field(
        default_factory=list,
        description='先筛选再汇总，格式同 query_sheet：{"column":"城市","op":"eq","value":"上海"}',
    )
    sort_metric: str = Field(
        default="", description="按哪个指标排序（写别名或列名，如「总金额」）；留空保持原顺序"
    )
    descending: bool = Field(default=True, description="true 为降序，通常「哪个最多」用降序")
    limit: int = Field(default=100, description="最多返回多少个分组，1..500")


@tool(args_schema=AggregateInput)
async def aggregate_sheet(
    path: str,
    sheet: str = "",
    group_by: str = "",
    fill_down: list[str] | None = None,
    metrics: list[dict[str, Any]] | None = None,
    filters: list[dict[str, Any]] | None = None,
    sort_metric: str = "",
    descending: bool = True,
    limit: int = 100,
) -> str:
    """对表格做汇总统计：求和、平均、计数、最大最小，可按列分组。

    支持 .xlsx / .xlsm / .csv / .tsv。用于「一共多少钱」「平均每单多少」
    「每个城市各多少单」这类问题。自己加数字容易错，尤其是行数超过几十行的
    时候——一律用这个工具算，包括 csv。

    分组列是合并单元格（只有首行有值、其余为空）时，填 ``fill_down`` 把它
    按上一行补齐，否则那些行会一起掉进「（空）」。
    """
    try:
        await _ask_grant(path)
        target = ensure_allowed(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    if not target.is_file():
        return _failed(f"不是文件：{target}", "DOC_READ_FAILED")
    if target.suffix.lower() not in SHEET_SUFFIXES:
        return _failed(
            f"只支持 {' / '.join(sorted(SHEET_SUFFIXES))}，不支持 {target.suffix or '无扩展名'}",
            "DOC_READ_FAILED",
        )

    rules = [rule for rule in (filters or []) if isinstance(rule, dict)]
    try:
        table = load_table(target, sheet)
        rows = apply_filters(table, rules) if rules else table.rows
        if fill_down:
            rows = fill_down_rows(rows, table, list(fill_down))
        result = aggregate(table, rows, list(metrics or []), group_by)
        result = sort_aggregate(result, sort_metric, descending)
    except DocError as exc:
        return _failed(str(exc), "DOC_READ_FAILED")

    head = f"「{table.sheet}」共 {len(table.rows)} 行数据"
    if rules:
        head += f"，筛选后 {len(rows)} 行（{describe(rules)}）"
    head += f"，按「{group_by}」分组汇总" if group_by else "，整表汇总"
    if sort_metric:
        head += f"，按 {sort_metric} {'降序' if descending else '升序'}"
    head += "："

    body = render_aggregate(result, min(500, max(1, limit)))
    lines = [head, body]
    if result.skipped:
        lines.append(f"注意：{result.skipped} 个单元格不是数字（文本或日期），没参与计算。")
    # 分组列有空值时**主动提示**怎么补：模型看不见原表的合并单元格，
    # 只看到一坨「（空）」，不说清楚它就会回一句「这列没法分组」然后罢工
    if group_by and not fill_down and any(line[0] == BLANK_GROUP for line in result.rows):
        try:
            blanks = blank_group_count(rows, table, group_by)
        except DocError:
            blanks = 0
        if blanks:
            lines.append(
                f"注意：「{group_by}」有 {blanks} 行是空的（多半是合并单元格，只有每组首行有值），"
                f"这些行被归到「{BLANK_GROUP}」。要按上一行的值补齐再统计，"
                f'加参数 fill_down=["{group_by}"] 重跑一次。'
            )
    return truncate_tool_output("\n".join(lines), limit=6000)


@tool(args_schema=QuerySheetInput)
async def query_sheet(
    path: str,
    sheet: str = "",
    filters: list[dict[str, Any]] | None = None,
    sort_by: str = "",
    descending: bool = False,
    limit: int = 50,
    save_copy: bool = False,
    save_to: str = "",
) -> str:
    """在表格里筛选和排序：返回符合条件的行，可选另存为副本。

    支持 .xlsx / .xlsm / .csv / .tsv。用于「按金额从高到低排」「找出大于 5000
    的订单」「有几个上海的客户」这类问题。只是查询时不会改文件；要留结果就传
    save_copy=true，会在副本里新增一页（csv 则另存一份「-筛选结果.csv」）。
    ``save_to`` 决定副本落在哪——用户说「存到桌面」就传桌面路径。
    """
    try:
        await _ask_grant(path)
        target = ensure_allowed(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    if not target.is_file():
        return _failed(f"不是文件：{target}", "DOC_READ_FAILED")
    if target.suffix.lower() not in SHEET_SUFFIXES:
        return _failed(
            f"只支持 {' / '.join(sorted(SHEET_SUFFIXES))}，不支持 {target.suffix or '无扩展名'}",
            "DOC_READ_FAILED",
        )

    rules = [rule for rule in (filters or []) if isinstance(rule, dict)]
    try:
        table = load_table(target, sheet)
        rows = apply_filters(table, rules)
        rows = sort_rows(table, rows, sort_by, descending)
    except DocError as exc:
        return _failed(str(exc), "DOC_READ_FAILED")

    head = f"「{table.sheet}」共 {len(table.rows)} 行数据"
    if rules:
        head += f"，筛选后 {len(rows)} 行（{describe(rules)}）"
    if sort_by:
        head += f"，按「{sort_by}」{'降序' if descending else '升序'}"
    if table.truncated:
        head += f"；表太大，只读了前 {MAX_DATA_ROWS} 行"

    shown = rows[: min(500, max(1, limit))]
    body = render(table, shown, len(shown))
    if len(rows) > len(shown):
        body += f"\n…（共 {len(rows)} 行，这里显示前 {len(shown)} 行）"

    lines = [head, body]

    if not save_copy:
        return truncate_tool_output("\n".join(lines), limit=6000)

    # 副本是「复制源文件 + 加一页」，扩展名必须跟源文件一致，否则会得到
    # 一个内容其实是 xlsx 的 .csv
    export_suffix = (
        target.suffix.lower() if target.suffix.lower() in DELIMITED_SUFFIXES else ".xlsx"
    )
    save_as = _with_suffix(
        _save_target(save_to, f"{target.stem}-筛选结果", export_suffix), export_suffix
    )
    where = f"，存到 {save_as}" if save_as is not None else ""
    try:
        await _confirm_with_grant(
            path,
            action="query_sheet",
            detail={
                "path": str(target),
                "preview": (
                    f"将在《{target.name}》的副本里新增一页「筛选结果」，"
                    f"写入 {len(rows)} 行（{describe(rules) or '全部数据'}）{where}。"
                    "原表和公式不会被改动。"
                ),
            },
            remember_key=f"query_sheet:{target}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        output = export_copy(target, table, rows, save_as=save_as)
    except DocError as exc:
        return _failed(str(exc), "DOC_EDIT_FAILED")

    lines.append(f"已另存为：{output}（原文件未改动，结果在「筛选结果」这一页）")
    return truncate_tool_output("\n".join(lines), limit=6000)


@tool(args_schema=WriteSheetInput)
async def write_sheet(
    path: str, rows: list[list[Any]], sheet: str = "Sheet1", open_after: bool = True
) -> str:
    """把数据写成一份**新表格文件**（.xlsx / .csv），可以直接生成到桌面。

    「整理成新表」「导出到桌面」「给我一份 Excel」都用这个：给全路径 + 二维
    数组（第一行是表头），**不需要先有源文件**，也不受原表所在目录限制。
    写完默认用系统默认程序打开，用户不用自己去桌面翻。

    同名文件已存在会自动加序号（汇总2.xlsx），绝不覆盖上一版结果。
    """
    try:
        absolute = _absolute(path)
        _pending_grant(str(absolute))
        target = absolute.resolve()
    except (PathNotAllowed, OSError, RuntimeError) as exc:
        message = getattr(exc, "message", str(exc))
        return _failed(message, getattr(exc, "code", "DOC_WRITE_FAILED"))

    if target.suffix.lower() not in {".xlsx", ".csv", ".tsv"}:
        return _failed(
            f"只能生成 .xlsx / .csv / .tsv，不支持 {target.suffix or '无扩展名'}",
            "DOC_WRITE_FAILED",
        )

    body_rows = max(0, len(rows) - 1)
    try:
        await _confirm_with_grant(
            path,
            action="write_sheet",
            detail={
                "path": str(target),
                "preview": (
                    f"将生成新表格《{target.name}》：{body_rows} 行数据 + 表头"
                    + ("，生成后用系统默认程序打开" if open_after else "")
                ),
            },
            # 记住的是**目录**而不是文件：同一批任务往往连着生成好几份，
            # 每份都弹一次确认会让人怀疑程序卡住了
            remember_key=f"write_sheet:{target.parent}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        output = write_table(target, rows, sheet)
    except DocError as exc:
        return _failed(str(exc), "DOC_WRITE_FAILED")

    lines = [f"已生成：{output}（{body_rows} 行数据 + 表头）"]
    if open_after:
        error = launch_default(output)
        lines.append(error or "已用系统默认程序打开，用户现在就能看到。")
    return truncate_tool_output("\n".join(lines), limit=2000)


DOCS_TOOLS = [
    read_document,
    digest_documents,
    query_sheet,
    aggregate_sheet,
    write_sheet,
    edit_document,
    organize_documents,
]
