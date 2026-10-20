"""提示词模板库：把「风格 / 构图 / 光线」这些专业约束固化下来。

为什么需要它：用户通常只会说「帮我生成一张三亚的打卡图」，
直接拿去生图往往得到平淡、构图松散的结果。模板把这些隐性要求显式化，
让不懂提示词的用户也能一次拿到可用的图（SPEC 1.4 的「套用模板生成同风格二创图」）。

模板只提供**创作约束**，不涉及任何平台数据，因此不依赖外部 API。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImageTemplate:
    """一个可复用的生图模板。"""

    key: str
    label: str
    scene: str
    style: str
    composition: str
    lighting: str
    example: str

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "scene": self.scene,
            "style": self.style,
            "composition": self.composition,
            "lighting": self.lighting,
            "example": self.example,
        }


TEMPLATES: tuple[ImageTemplate, ...] = (
    ImageTemplate(
        key="checkin",
        label="旅行打卡",
        scene="景点前的第一视角或半身打卡照",
        style="真实感旅游摄影，色彩饱满但不过度饱和",
        composition="主体位于画面三分线交点，保留开阔环境交代地点",
        lighting="自然日光，轻微逆光轮廓，阴影柔和",
        example="三亚亚龙湾海边栈道打卡照，远处海面泛光，旅行博主视角",
    ),
    ImageTemplate(
        key="poster",
        label="旅行海报",
        scene="用于朋友圈/社交平台分享的竖版海报",
        style="现代平面设计，留白充足，可容纳标题文字区域",
        composition="竖版构图，上方留出标题空间，主体位于中下部",
        lighting="干净柔光，色调统一，弱对比",
        example="三亚五天四晚旅行海报，椰林与海岸剪影，上方留白放标题",
    ),
    ImageTemplate(
        key="note",
        label="笔记封面",
        scene="攻略类图文的首图封面",
        style="清新小红书风格，浅色调，生活感强",
        composition="俯拍或平视，画面简洁，主体占画面六成",
        lighting="明亮均匀的散射光，避开硬阴影",
        example="三亚住宿攻略封面，靠海阳台与早餐托盘，晨光",
    ),
    ImageTemplate(
        key="food",
        label="美食特写",
        scene="地方小吃与海鲜的近距离特写",
        style="美食摄影，质感细腻，突出食材纹理",
        composition="45 度俯拍近景，浅景深虚化背景",
        lighting="侧逆光勾勒光泽，暖色调",
        example="三亚第一市场海鲜加工，蒜蓉蒸虾特写，桌面木质纹理",
    ),
    ImageTemplate(
        key="night",
        label="夜景氛围",
        scene="海边或城市的夜晚氛围图",
        style="电影感夜景，冷暖对比明显",
        composition="低机位，前景有引导线（栈道、栏杆）",
        lighting="环境光为主，点缀暖色光源，长曝光质感",
        example="三亚湾夜景，远处灯光倒映海面，栈道引导视线",
    ),
    ImageTemplate(
        key="portrait",
        label="旅拍人像",
        scene="旅途中的自然人物肖像",
        style="自然写真，肤色真实，不过度磨皮",
        composition="中景半身，人物朝向留白一侧",
        lighting="柔和自然光，避免顶光造成的深眼窝",
        example="蜈支洲岛海边旅拍，白色连衣裙，微风，侧光",
    ),
)

_BY_KEY = {item.key: item for item in TEMPLATES}

TEMPLATE_KEYS: tuple[str, ...] = tuple(item.key for item in TEMPLATES)


def find_template(key: str) -> ImageTemplate | None:
    """按 key 取模板；大小写与首尾空白不敏感。"""
    return _BY_KEY.get((key or "").strip().lower())


def compose_prompt(prompt: str, template: ImageTemplate | None) -> str:
    """把用户意图与模板约束拼成最终提示词。

    用户原句放最前面：模型对靠前的描述权重更高，
    模板只做风格与技术要求，不能盖掉用户真正想画的东西。
    """
    base = (prompt or "").strip()
    if template is None:
        return base
    parts = [
        base,
        f"风格：{template.style}",
        f"构图：{template.composition}",
        f"光线与色调：{template.lighting}",
    ]
    return "；".join(part for part in parts if part)


def describe_templates() -> str:
    """给模型看的模板清单。"""
    lines = ["可用生图模板（style 参数填 key）："]
    for item in TEMPLATES:
        lines.append(
            f"- {item.key}｜{item.label}｜适用：{item.scene}｜"
            f"风格：{item.style}｜示例：{item.example}"
        )
    return "\n".join(lines)
