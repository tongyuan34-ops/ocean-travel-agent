"""把上游的生图报错翻译成人话。

上游返回的是 `HTTP 403：{'error': {'code': 'AccountOverdueError', ...}}`
这种字符串。原样丢给用户等于让他自己猜"是不是网络问题、要不要重试"——
而这条错误的正确答案是「欠费，重试无效」。翻译放在这里而不是
工具或路由里，是因为两个出口都要用：对话里的工具返回值，
和前端轮询任务状态时看到的 error 字段。同一条错误在两处说两种话，
比说错更糟。
"""

from __future__ import annotations

_BRIEF_LIMIT = 160


def _brief(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= _BRIEF_LIMIT else flat[:_BRIEF_LIMIT] + "…"


def explain_media_error(raw: str) -> str:
    """一句话说清「出了什么事」和「下一步该做什么」，末尾保留原始片段供排查。"""
    text = (raw or "").strip()
    lowered = text.lower()
    if "overdue" in lowered or "欠费" in text or "balance" in lowered:
        return (
            f"生图账号欠费，图片没有生成。请到服务商控制台补缴后再试，"
            f"重试不会恢复。（{_brief(text)}）"
        )
    # 内容审核拦下。这一条要排在最前面判，因为它的 type 是 BadRequest、
    # 文案里又带 request，很容易被后面的分支误判成"参数错了"
    if (
        "sensitivecontent" in lowered
        or "policyviolation" in lowered
        or "copyright" in lowered
        or "内容审核" in text
    ):
        return (
            "生成结果被内容审核拦下，通常是版权或敏感内容。"
            "提示词里如果写了具体的卡通形象、影视角色、明星或品牌 logo，"
            "换成通用描述再试——这条改账号、重试都不会通过。（"
            f"{_brief(text)}）"
        )
    if "invalidparameter" in lowered or "invalid parameter" in lowered:
        return f"生图参数不被接受（多半是尺寸或模型名不对）。（{_brief(text)}）"
    if "unauthorized" in lowered or "forbidden" in lowered or " 401" in text or " 403" in text:
        return (
            f"生图服务拒绝了当前 Key（鉴权失败）。"
            f"请检查 ARK_API_KEY 是否有效、是否有该模型的权限。（{_brief(text)}）"
        )
    if "rate limit" in lowered or "429" in text or "限流" in text:
        return f"生图服务限流，稍等一会儿再试。（{_brief(text)}）"
    if "timeout" in lowered or "timed out" in lowered or "超时" in text:
        return f"生图服务超时，没有拿到结果。（{_brief(text)}）"
    return f"生图任务失败：{_brief(text) or '上游没有给出原因'}"
