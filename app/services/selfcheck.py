"""本轮自检：模型说了却没做，必须当场指出来。

生图是最容易出事的一类。工具是异步的，模型只要说一句「三张图都提交好了」，
用户就会开始等一张永远不会来的图——而实际上它一次工具都没调。
这不是模型偶尔手滑，是「异步 + 模型自述」这个组合的固有风险：
任务有没有真的创建，只有工具调用记录知道，模型的自述不能当证据。

所以这里只做一件事：把三个条件同时成立的那一轮识别出来——
用户要图、回答里声称已提交、本轮没有 generate_image 调用。

判定刻意保守。少任何一条都可能只是正常的澄清追问
（比如「你想画什么风格？」「要几张？」），误报比漏报更伤信任：
用户会开始怀疑所有提示，包括真的那条。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 用户这一轮确实在要图。用句式而不是关键词：
# 「生成三张旅行图」「给我出几张图」「画一张海边日落」中间隔着量词，
# 单纯找「生成图」这种连续词一个都匹配不上——第一版就是这么漏的。
# 还有一类连「图」字都没有：「生成每日路线以及可能看到的风景总共三张」，
# 只能靠量词认。量词后面排除「表/格/单」，免得把「生成两张表格」算成要图。
_IMAGE_INTENT_RE = re.compile(
    r"(?:生成|画|做|出|来|配)[^。！？\n]{0,20}(?:图|图片)"  # 生成三张旅行图 / 出几张图
    r"|(?:生成|画|出|做)[^。！？\n]{0,20}(?:三张|两张|几张|一张|多张|数张)(?![表格单])"
    r"|画(?:一张|张|个|一幅|幅)"  # 画一张海边日落
    r"|生图|出图|配图"
)

# 回答里声称已经做了。这些词出现，说明模型在向用户汇报"动作已完成"。
# 「提交」单独一个词不敢收（「你可以提交」是建议，不是汇报），
# 但「提交生成」「提交好」这类组合可以
_CLAIM_DONE: tuple[str, ...] = (
    "已提交",
    "提交了",
    "提交生成",
    "提交好",
    "已创建",
    "生成中",
    "正在生成",
    "已生成",
    "已经生成",
    "生成好",
    "出图后",
    "排队生成",
)


def _hits(text: str, words: Iterable[str]) -> list[str]:
    return [word for word in words if word in text]


def has_image_intent(text: str) -> bool:
    return bool(_IMAGE_INTENT_RE.search(text or ""))


def check_turn(
    *,
    user_text: str,
    answer: str,
    tool_names: Iterable[str],
    media_task_ids: Iterable[str],
) -> list[dict[str, str]]:
    """返回本轮应当显示给用户的告警；空列表表示没有发现问题。

    tool_names 是本轮真正调用过的工具名，media_task_ids 是从工具结果里
    解析出来的任务 id——两者都为空的「已提交」一定是假的。
    """
    names = set(tool_names)
    task_ids = list(media_task_ids)
    if "generate_image" in names or task_ids:
        # 真的调了，哪怕后来失败了，也轮不到这里说话：
        # 失败原因由任务卡片自己报，那比这里准
        return []

    if not has_image_intent(user_text):
        return []
    claim = _hits(answer, _CLAIM_DONE)
    if not claim:
        return []

    matched = _IMAGE_INTENT_RE.search(user_text or "")
    return [
        {
            "code": "CLAIMED_WITHOUT_ACTION",
            "title": "这一轮没有真的提交生图任务",
            "message": (
                "模型说图片已经提交，但本轮没有调用生图工具，"
                "所以没有任何图片在生成，等下去也不会有结果。"
                "请点下方的「重新生成」，或用生图面板手动提交。"
            ),
            "evidence": (
                f"用户要图（{matched.group(0) if matched else ''}）· "
                f"回答声称完成（{'/'.join(claim[:2])}）· 工具调用 0 次"
            ),
        }
    ]
