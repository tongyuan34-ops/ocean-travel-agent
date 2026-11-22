"""按轮次选择工具子集（工具路由）。

**为什么需要这一层**

32 个工具的 JSON Schema 合起来约 13k token，而 ``bind_tools`` 是**每轮请求
都要发一遍**的：它不像消息历史那样会滚动，也不像摘要那样会被折叠。实测一句
「你好」的固定输入是 16,233 token，其中 13,144 来自工具定义——用户什么都没问，
先付了一万三千 token 的钱，还顺带把首字延迟拖长了。

**为什么不干脆砍工具**

工具本身都有用，问题在「什么时候给」。用户问「你好」时给模型一份
``aggregate_sheet`` 的参数说明，它既不会调用，也读不懂为什么要读。所以这里
按**能力域**分组，每轮只把可能用到的几组交给模型。

**为什么不直接用 LLM 做意图分类**

那要多花一次模型调用，而且是在**首 token 之前**多花——延迟和成本都更差，
判断还不稳定（同一句话两次分类结果可能不同）。这里用关键词与正则，
纯 CPU、微秒级、可被单测完全钉住。

**猜错了怎么办**

这是本方案唯一的风险点，用三层兜底：

1. 关键词宁可**多绑一组**，也不轻易漏——多绑一组约 300~900 token，
   比「模型想调却没有工具」便宜得多；
2. 本轮没命中任何组时**沿用上一轮的组**（``active``），追问场景不会掉工具；
3. 真的没有合适工具时，模型可以调 ``discover_tools`` 说明意图，它会把命中的
   组**激活到会话**，下一轮自动可用。

``TOOL_ROUTING=all`` 可以整体关掉这一层，退回全量绑定（排查问题用）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass

from langchain_core.tools import BaseTool

# 一轮最多给模型多少个工具。上限存在的意义不是省 token，而是**别退化成全量**：
# 全命中时 32 个工具全给回去，这层就白做了。
MAX_TOOLS = 16

# 常驻的元工具。它们不在任何组里，任何一轮都绑——否则模型想调工具时
# 连「怎么申请工具」「怎么取技能」都不知道。
# ``META_TOOL`` 保留单数别名：它是最早的那个元工具，测试与脚本按这个名字取。
META_TOOL = "discover_tools"
META_TOOLS = frozenset({META_TOOL, "use_skill"})


@dataclass(frozen=True)
class ToolGroup:
    """一组能力相近的工具。

    ``triggers`` 是子串匹配（中文不需要分词），``patterns`` 给「4 天」这种
    带数字的形态用——写成子串会退化成匹配单字「天」，误伤「今天」。
    """

    key: str
    label: str
    summary: str
    triggers: tuple[str, ...]
    tools: tuple[str, ...]
    patterns: tuple[str, ...] = ()


# 顺序即优先级：命中数相同时，靠前的组优先占 MAX_TOOLS 的名额。
GROUPS: tuple[ToolGroup, ...] = (
    ToolGroup(
        key="trip",
        label="行程规划",
        summary="多天行程串线、查天气与路线、景点检索、预算测算、行程规则库",
        triggers=(
            "行程",
            "规划",
            "怎么玩",
            "玩几天",
            "路线",
            "景点",
            "攻略",
            "自由行",
            "自驾",
            "游玩",
            "打卡",
            "去哪",
            "好玩",
            "旅游",
            "旅行",
            "出行",
            "想去",
            "要去",
            "打算去",
            "准备去",
            "周末",
            "亲子",
            "带娃",
            "值得去",
            "门票",
            "开放时间",
            "节奏",
            "几天",
        ),
        patterns=(r"\d+\s*天", r"第[一二三四五六七1-7]\s*天"),
        tools=(
            "plan_trip",
            "search_poi",
            "get_weather",
            "plan_route",
            "optimize_day_route",
            "estimate_budget",
            "search_knowledge",
        ),
    ),
    ToolGroup(
        key="stay",
        label="住宿与交通",
        summary="按预算和区域找具体酒店、对比两地交通方式与里程耗时",
        triggers=(
            "住哪",
            "酒店",
            "民宿",
            "住宿",
            "订房",
            "房型",
            "大床",
            "双床",
            "几晚",
            "一晚",
            "怎么去",
            "机票",
            "高铁",
            "火车",
            "航班",
            "打车",
            "包车",
            "交通",
            "多远",
            "开车",
            "步行",
            "骑行",
            "里程",
            "距离",
            "地铁",
        ),
        tools=("recommend_stay", "recommend_transport"),
    ),
    ToolGroup(
        key="delivery",
        label="行程交付",
        summary="把已定稿的行程排成 A4 PDF 行程单，或生成带编号连线的路线图",
        triggers=("pdf", "行程单", "路线图", "打印", "导出成", "发我一份"),
        tools=("export_trip_pdf", "render_trip_map"),
    ),
    ToolGroup(
        key="docs",
        label="文档与表格",
        summary="读 Word/Excel 结构、筛选排序、分组汇总、生成新表、副本式编辑、批量整理",
        triggers=(
            "表格",
            "表里",
            "这个表",
            "这张表",
            "那张表",
            "文档",
            "excel",
            "xlsx",
            "csv",
            "word",
            "docx",
            "合同",
            "报价单",
            "名单",
            "统计",
            "汇总",
            "合计",
            "总计",
            "求和",
            "平均",
            "筛选",
            "排序",
            "排一下",
            "排个序",
            "金额",
            "多少条",
            "多少个",
            "几单",
            "客户",
            "订单",
            "工作表",
            "另存",
            "新表",
            "表头",
            "透视",
            "计数",
            "这些数据",
            "整理",
            "归类",
            "分个类",
            "归到",
        ),
        tools=(
            "digest_documents",
            "read_document",
            "query_sheet",
            "aggregate_sheet",
            "write_sheet",
            "edit_document",
            "organize_documents",
        ),
    ),
    ToolGroup(
        key="file",
        label="本机文件",
        summary="列目录、读文件、写文件、建目录、删空目录、设壁纸、用默认程序打开",
        triggers=(
            "文件",
            "目录",
            "文件夹",
            "打开",
            "读取",
            "写入",
            "删除",
            "新建",
            "壁纸",
            "桌面",
            "路径",
            "保存到",
            "存到",
        ),
        tools=(
            "list_dir",
            "read_file",
            "open_file",
            "write_file",
            "create_dir",
            "delete_path",
            "set_wallpaper",
        ),
    ),
    ToolGroup(
        key="web",
        label="公开网页检索",
        summary="必应检索、抓取用户给出的链接正文、解析分享链接",
        triggers=(
            "搜一下",
            "搜索",
            "查一下",
            "网址",
            "链接",
            "网页",
            "官网",
            "新闻",
            "政策",
            "最新消息",
            "公告",
            "公开资料",
        ),
        tools=("search_web", "fetch_url", "parse_shared_link"),
    ),
    ToolGroup(
        key="social",
        label="社媒内容采集",
        summary="在已配置登录态的抖音/小红书等平台检索真实体验与口碑，或生成采集脚本草案",
        triggers=(
            "小红书",
            "抖音",
            "b站",
            "微博",
            "知乎",
            "贴吧",
            "种草",
            "口碑",
            "真实体验",
            "本地人",
            "测评",
            "避坑",
        ),
        tools=("search_social_session", "generate_scraper"),
    ),
    ToolGroup(
        key="media",
        label="图片生成",
        summary="按描述出图、套风格模板、以用户上传的图为底图改图",
        triggers=(
            "画一",
            "画个",
            "生成图",
            "图片",
            "照片",
            "海报",
            "头像",
            "插画",
            "生图",
            "修图",
            "参考图",
            "做一张",
            "背景图",
        ),
        tools=("generate_image", "list_image_templates"),
    ),
)

_BY_KEY = {group.key: group for group in GROUPS}
ALL_GROUP_KEYS: tuple[str, ...] = tuple(group.key for group in GROUPS)

# 附件类型 → 组。用户传了 xlsx 却只说「看看」，关键词里一个都不沾，
# 但显然要用 docs 组，所以按扩展名补一条。
_ATTACHMENT_GROUPS: dict[str, str] = {
    ".xlsx": "docs",
    ".xlsm": "docs",
    ".csv": "docs",
    ".tsv": "docs",
    ".docx": "docs",
    ".doc": "docs",
    ".png": "media",
    ".jpg": "media",
    ".jpeg": "media",
    ".webp": "media",
}


def _compile(group: ToolGroup) -> list[re.Pattern[str]]:
    return [re.compile(pattern) for pattern in group.patterns]


_PATTERNS: dict[str, list[re.Pattern[str]]] = {g.key: _compile(g) for g in GROUPS}


def _hits(text: str) -> dict[str, int]:
    """统计每个组命中了多少个信号。命中数用于排序，也用于截断时取舍。"""
    lowered = (text or "").lower()
    if not lowered:
        return {}
    scores: dict[str, int] = {}
    for group in GROUPS:
        score = 0
        for word in group.triggers:
            if word in lowered:
                score += 1
        for pattern in _PATTERNS[group.key]:
            if pattern.search(lowered):
                score += 1
        if score:
            scores[group.key] = score
    return scores


def groups_for_attachments(names: Iterable[str]) -> set[str]:
    """按附件扩展名推出需要的组。"""
    found: set[str] = set()
    for name in names:
        suffix = ("." + str(name).rsplit(".", 1)[-1].lower()) if "." in str(name) else ""
        key = _ATTACHMENT_GROUPS.get(suffix)
        if key:
            found.add(key)
    return found


def route(
    text: str,
    *,
    persona: str | None = None,
    attachments: Iterable[str] = (),
    active: Iterable[str] = (),
) -> list[str]:
    """算出本轮该绑哪几组。

    ``active`` 是上一轮激活的组：本轮一个信号都没命中时沿用它，
    让「那第二天呢」这种纯追问不至于掉工具。命中新信号时**不**沿用——
    用户从表格转到行程，还留着 docs 组只是白花钱。
    """
    # 闲聊人格不该看到旅行工具，否则话题会被工具描述拽回主业
    if (persona or "").strip().lower() == "confidant":
        return []

    scores = _hits(text)
    for key in groups_for_attachments(attachments):
        scores[key] = scores.get(key, 0) + 2  # 附件是强信号，压过单个关键词

    if not scores:
        return [key for key in active if key in _BY_KEY]

    ordered = sorted(scores.items(), key=lambda item: (-item[1], ALL_GROUP_KEYS.index(item[0])))
    picked: list[str] = []
    budget = MAX_TOOLS
    for key, _score in ordered:
        size = len(_BY_KEY[key].tools)
        if size > budget and picked:
            continue
        picked.append(key)
        budget -= size
        if budget <= 0:
            break
    return picked


def names_for(keys: Iterable[str]) -> list[str]:
    """组 → 工具名（按组顺序展开，保证稳定）。"""
    wanted = set(keys)
    names: list[str] = []
    for group in GROUPS:
        if group.key in wanted:
            names.extend(group.tools)
    return names


def tools_for(
    keys: Iterable[str],
    pool: Sequence[BaseTool],
    *,
    routing: str = "auto",
) -> list[BaseTool]:
    """组 → 工具对象。

    ``routing="all"`` 是逃生舱：整体关掉路由，退回全量绑定。
    输出顺序跟随 ``pool``，让同一组输入永远得到同一个列表——
    图缓存的 key 依赖这个稳定性。
    """
    if (routing or "auto").strip().lower() == "all":
        return list(pool)

    wanted = set(names_for(keys))
    wanted.update(META_TOOLS)
    return [tool for tool in pool if tool.name in wanted]


def select_tools(
    text: str,
    *,
    pool: Sequence[BaseTool],
    persona: str | None = None,
    attachments: Iterable[str] = (),
    active: Iterable[str] = (),
    routing: str = "auto",
) -> list[BaseTool]:
    """一句话搞定「选组 + 取工具」，给脚本和单测用。"""
    keys = route(text, persona=persona, attachments=attachments, active=active)
    return tools_for(keys, pool, routing=routing)


def describe(keys: Iterable[str] | None = None) -> str:
    """给 ``discover_tools`` 用的能力清单。"""
    active = set(keys or ())
    lines = []
    for group in GROUPS:
        mark = "（本轮已可用）" if group.key in active else ""
        lines.append(
            f"- {group.key}｜{group.label}{mark}：{group.summary}\n  工具：{'、'.join(group.tools)}"
        )
    return "\n".join(lines)


# ---------- 跨轮激活 ----------

# 与 ``current_broker`` / ``current_media`` 同一套做法：工具在后台任务里执行，
# ContextVar 由任务继承，传进去的可变集合是**同一个对象**，所以工具往里加组，
# 主流程在流结束后能读到。
_active_groups: ContextVar[set[str] | None] = ContextVar("toolset_active", default=None)


def bind_active(keys: Iterable[str]) -> set[str]:
    """在当前上下文里挂一个可变集合，返回同一个对象供主流程回读。"""
    box = set(keys)
    _active_groups.set(box)
    return box


def activate(keys: Iterable[str]) -> list[str]:
    """把组激活到当前轮。返回真正新增的组，供工具回报。

    没有绑上下文（脚本直接调工具、单测）时按「已激活」返回：
    那时本来就没有路由，报「没激活」只会误导调用方。
    """
    valid = [key for key in keys if key in _BY_KEY]
    box = _active_groups.get()
    if box is None:
        return valid
    added = [key for key in valid if key not in box]
    box.update(added)
    return added


def known(key: str) -> bool:
    return key in _BY_KEY


__all__ = [
    "ALL_GROUP_KEYS",
    "GROUPS",
    "MAX_TOOLS",
    "META_TOOL",
    "META_TOOLS",
    "ToolGroup",
    "activate",
    "bind_active",
    "describe",
    "groups_for_attachments",
    "known",
    "names_for",
    "route",
    "select_tools",
    "tools_for",
]
