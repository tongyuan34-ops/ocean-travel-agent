"""行前清单技能的工具：把模板和条件项拼成一份可勾选的清单。

**为什么返回正文，而不是直接写文件**

写文件要先解决「写到哪个目录、用户有没有授权」——那套流程在 ``write_file``
里已经完整实现（按需授权、确认弹窗、沙箱校验）。技能工具在这里再写一遍，
只会多出一个会漂移的权限入口。所以工具只负责「把清单生成对」，要不要落盘
交给用户决定，再由 ``write_file`` 走它自己的确认流程。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

_TEMPLATE = Path(__file__).resolve().parent / "templates" / "packing-list.md"

# 基础项：任何旅行都要带的东西
_BASE: dict[str, tuple[str, ...]] = {
    "证件与票据": (
        "身份证 / 护照（按目的地）",
        "车票 / 机票 / 酒店确认单",
        "少量现金与银行卡",
        "驾照（自驾时）",
    ),
    "衣物": (
        "按天数准备换洗衣物",
        "外套或薄羽绒（早晚温差）",
        "舒适的走路鞋",
        "睡衣与袜子",
    ),
    "洗漱与护肤": (
        "牙刷牙膏 / 洗面奶",
        "防晒霜",
        "纸巾与湿巾",
        "常用护肤品（分装）",
    ),
    "电子设备": (
        "手机与充电器",
        "充电宝（随身携带，不可托运）",
        "耳机",
        "转换插头（出境时）",
    ),
    "药品": (
        "个人常用药（按天数 + 2 天备量）",
        "创可贴 / 消毒湿巾",
        "晕车药（山路或轮渡）",
        "肠胃药与退烧药",
    ),
    "其他": (
        "折叠伞或雨衣",
        "水杯",
        "小背包（日间出行）",
        "证件复印件 / 电子备份",
    ),
}

# 条件项：命中关键词才追加，避免所有人都拿到一份八十项的大清单
_CONDITIONAL: tuple[tuple[tuple[str, ...], str, tuple[str, ...]], ...] = (
    (
        ("小孩", "儿童", "宝宝", "孩子", "亲子"),
        "带小孩追加",
        (
            "儿童餐具与围兜",
            "退烧药与体温计（按年龄）",
            "推车或背带（按行程强度）",
            "换洗衣物 +2 套",
            "小零食与安抚玩具",
        ),
    ),
    (
        ("老人", "长辈", "父母", "爸妈"),
        "带长辈追加",
        (
            "常用药与病历复印件",
            "折叠凳或轻便拐杖",
            "保温杯",
            "行程留出午休时间（写进备注）",
        ),
    ),
    (
        ("海边", "海岛", "沙滩", "潜水", "浮潜", "三亚", "厦门", "青岛", "北海"),
        "海边追加",
        (
            "泳衣 / 泳镜",
            "防水袋（手机与证件）",
            "沙滩拖鞋",
            "晒后修复",
        ),
    ),
    (
        ("徒步", "爬山", "登山", "露营", "高原"),
        "户外追加",
        (
            "登山鞋与速干衣",
            "护膝",
            "头灯或手电",
            "能量补给",
        ),
    ),
    (
        ("自驾", "租车", "开车"),
        "自驾追加",
        (
            "驾照与行驶证",
            "车载充电器与支架",
            "离线地图",
        ),
    ),
    (
        ("冬", "雪", "滑雪", "冰"),
        "冬季追加",
        (
            "保暖内衣与厚袜",
            "手套、帽子、围巾",
            "润唇膏与身体乳",
        ),
    ),
)


def _collect(companions: list[str], activities: list[str], destination: str) -> list[str]:
    """按同行人 / 活动 / 目的地挑条件项，返回成段的 Markdown。"""
    haystack = " ".join([*companions, *activities, destination]).lower()
    sections: list[str] = []
    for heading, items in _BASE.items():
        lines = [f"## {heading}", *(f"- [ ] {item}" for item in items), ""]
        sections.append("\n".join(lines))

    extra: list[tuple[str, tuple[str, ...]]] = []
    for words, label, items in _CONDITIONAL:
        if any(word.lower() in haystack for word in words):
            extra.append((label, items))
    if extra:
        lines = ["## 按本次行程追加"]
        for label, items in extra:
            lines.append(f"### {label}")
            lines.extend(f"- [ ] {item}" for item in items)
        sections.append("\n".join(lines) + "\n")
    return "\n".join(sections)


def _render(values: dict[str, str]) -> str:
    """套模板。模板缺失时退回一段最小可用的正文，不让技能整体失效。"""
    try:
        template = _TEMPLATE.read_text(encoding="utf-8-sig")
    except OSError:
        template = "# {{destination}} 行前清单\n\n{{sections}}\n"
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


class PackingListInput(BaseModel):
    destination: str = Field(description="目的地，如「三亚」")
    days: int = Field(default=3, description="出行天数，1..30")
    season: str = Field(default="", description="季节或出行月份，如「10 月」「冬季」")
    companions: list[str] = Field(
        default_factory=list, description="同行人，如「小孩」「老人」「伴侣」"
    )
    activities: list[str] = Field(
        default_factory=list, description="主要活动，如「海边」「徒步」「自驾」「城市漫步」"
    )


@tool(args_schema=PackingListInput)
def build(
    destination: str,
    days: int = 3,
    season: str = "",
    companions: list[str] | None = None,
    activities: list[str] | None = None,
) -> str:
    """按项目模板生成行前清单正文（含条件项）。

    只生成文本，不写文件。把正文直接展示给用户；用户明确要文件时，
    再调用 write_file 保存到用户指定的路径。
    """
    place = (destination or "").strip()
    if not place:
        return "INVALID_REQUEST：目的地不能为空"

    count = max(1, min(int(days or 3), 30))
    people = [str(item).strip() for item in (companions or []) if str(item).strip()]
    acts = [str(item).strip() for item in (activities or []) if str(item).strip()]
    values = {
        "destination": place,
        "days": str(count),
        "season": (season or "按常年气候准备").strip(),
        "companions": "、".join(people) or "未说明",
        "activities": "、".join(acts) or "城市漫步",
        "sections": _collect(people, acts, place),
    }
    return _render(values)


TOOLS = [build]
