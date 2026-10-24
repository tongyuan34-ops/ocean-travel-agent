"""media 组工具：生图（SPEC 4.6 media 组）。

**刻意不在这里等出图结果**：生图是慢任务，SPEC 2.2 明确要求
「生图/生视频为慢任务，统一走异步任务，不得占用聊天 SSE 流」。
所以工具只负责提交任务并返回 task_id，由前端轮询
GET /api/v1/media/tasks/{task_id} 更新卡片状态。
若在这里 await 到底，用户会盯着一个卡住的对话流看几十秒。

任务必须提交到**当前会话的 MediaService 实例**（经 ContextVar 取得），
否则返回的 task_id 在任务查询接口上查不到——这是最容易被忽略的一致性要求。
"""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.config import load_settings
from app.core.context import truncate_tool_output
from app.media.base import MediaError, MediaKind, MediaRequest
from app.media.service import current_media
from app.media.templates import (
    TEMPLATE_KEYS,
    compose_prompt,
    describe_templates,
    find_template,
)

_LIST_LIMIT = 2200


class GenerateImageInput(BaseModel):
    prompt: str = Field(description="画面描述，例如「三亚亚龙湾海边栈道打卡照，远处海面泛光」")
    style: str = Field(
        default="",
        description=(
            f"风格模板 key（{'/'.join(TEMPLATE_KEYS)}），或直接写自由风格描述；留空表示不加风格约束"
        ),
    )
    ref_image: str = Field(default="", description="参考图 URL（可选，用于图生图/风格参考）")


@tool(args_schema=GenerateImageInput)
async def generate_image(prompt: str, style: str = "", ref_image: str = "") -> str:
    """提交一个生图任务，立即返回 task_id；出图结果需由前端轮询任务接口获取。

    只负责创建任务，不会阻塞等待图片生成完成。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "INVALID_REQUEST：请提供画面描述（prompt）"

    service = current_media.get()
    if service is None:
        return "TOOL_NOT_ENABLED：生图工具只能在会话中使用（当前没有可用的媒体服务上下文）"

    caps = service.capabilities()
    if not caps.get("image_gen"):
        reasons = service.unavailable_reasons()
        detail = reasons.get("image", "未知原因")
        return f"MEDIA_BACKEND_MISSING：生图后端不可用（{detail}）"

    template = find_template(style)
    # style 不是已知模板时，当作自由风格描述处理，而不是报错——
    # 用户可能确实想自定义风格
    free_style = "" if template is not None else style.strip()
    final_prompt = compose_prompt(prompt, template)
    if free_style:
        final_prompt = f"{final_prompt}；风格：{free_style}"

    try:
        task = await service.submit(
            MediaRequest(
                kind=MediaKind.IMAGE,
                prompt=final_prompt,
                style=template.key if template else (free_style or None),
                ref_image=ref_image.strip() or None,
            )
        )
    except MediaError as exc:
        # 未配置后端等错误原样透传错误码，不伪造成功
        return f"{exc.code}：{exc.message}"
    except Exception as exc:  # noqa: BLE001 - 工具不得把异常抛给模型
        return f"TOOL_FAILED：生图任务创建失败（{exc}）"

    lines = [
        "已创建生图任务（异步执行，不会阻塞对话）",
        f"task_id：{task.id}",
        f"状态：{task.status.value}",
    ]
    if template is not None:
        lines.append(f"已套用模板：{template.label}（{template.key}）")
    if ref_image.strip():
        lines.append(f"参考图：{ref_image.strip()}")
    lines.append(f"实际提示词：{final_prompt}")
    lines.append("")
    lines.append("请告知用户：图片生成中，前端会自动轮询任务状态并在完成后展示结果。")
    lines.append("注意：返回的图片是 24 小时内失效的临时链接，需要长期保存的要及时另存。")
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)


@tool
async def list_image_templates() -> str:
    """列出可用的生图风格模板，供选择后用于 generate_image 的 style 参数。"""
    settings = load_settings()
    lines = [describe_templates()]
    lines.append("")
    if settings.image_backend == "none":
        lines.append("提示：当前未配置生图后端（IMAGE_BACKEND=none），模板只能用于构思。")
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)
