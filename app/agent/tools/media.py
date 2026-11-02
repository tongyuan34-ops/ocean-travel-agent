"""media 组工具：生图（SPEC 4.6 media 组）。

**刻意不在这里等出图结果**：生图是慢任务，SPEC 2.2 明确要求
「生图/生视频为慢任务，统一走异步任务，不得占用聊天 SSE 流」。
所以工具只负责提交任务并返回 task_id，由前端轮询
GET /api/v1/media/tasks/{task_id} 更新卡片状态。
若在这里 await 到底，用户会盯着一个卡住的对话流看几十秒。

任务必须提交到**当前会话的 MediaService 实例**（经 ContextVar 取得），
否则返回的 task_id 在任务查询接口上查不到——这是最容易被忽略的一致性要求。

**提交后会观察约一秒**（见 `_probe_early_failure`）：不等出图，只抓
「当场就失败」的那类错误。欠费、Key 失效、模型未开通都是确定性的，
上游拒绝实测在 0.2 秒内就返回；如果不等这一下，工具会返回「已创建任务」，
模型照着说明对用户说「生成中」，用户就对着一个已经死掉的任务等下去。
这个故障真实发生过：账号欠费，连续四轮对话都在说「已提交」，
而页面上从来没有出现过一张图。
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.config import load_settings
from app.core.context import truncate_tool_output
from app.media.base import MediaError, MediaKind, MediaRequest, TaskStatus
from app.media.errors import explain_media_error
from app.media.service import current_media
from app.media.templates import (
    TEMPLATE_KEYS,
    compose_prompt,
    describe_templates,
    find_template,
)

_LIST_LIMIT = 2200

# 提交后观察多久。只用来抓确定性失败，不是等出图（出图要十几到几十秒）。
# 实测上游拒绝（401/403）在 0.2 秒内就返回，1 秒足够兜住，
# 再长就是拿用户的时间换一点保险，不划算
_EARLY_FAIL_SEC = 1.0
_EARLY_FAIL_STEP = 0.1


async def _probe_early_failure(service, task_id: str) -> str:
    """观察一秒：已经失败就返回人话原因，还在跑就返回空串放行。

    刻意不 await 到出图完成——那是十几秒起步的慢任务，
    工具里等它等于把对话流卡住。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _EARLY_FAIL_SEC
    while True:
        task = await service.get(task_id)
        if task is not None:
            if task.status is TaskStatus.FAILED:
                return explain_media_error(task.error or "")
            if task.status is TaskStatus.SUCCEEDED:
                # 桩后端或极快的上游：已经出图了，没必要再等
                return ""
        if loop.time() >= deadline:
            return ""
        await asyncio.sleep(_EARLY_FAIL_STEP)


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
    """提交一个生图任务并返回 task_id；出图结果由前端轮询任务接口获取。

    提交后会花约一秒确认上游没有当场拒绝，但**不会**等待出图完成。
    如果上游立即失败（欠费、鉴权、模型未开通），直接返回错误码而不是 task_id——
    这种情况不要告诉用户「图片生成中」，因为根本没有图片会生成。
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

    # 提交成功不等于能出图。这里花两秒确认它没有当场死掉，
    # 否则模型会把「已创建」当成「已生成」讲给用户
    failure = await _probe_early_failure(service, task.id)
    if failure:
        return f"MEDIA_UPSTREAM_ERROR：{failure}"

    # 探测期间状态可能已经推进，重读一次，别把提交那一刻的 pending 当结论
    latest = await service.get(task.id)
    status_text = latest.status.value if latest is not None else task.status.value

    lines = [
        "已创建生图任务（异步执行，不会阻塞对话）",
        f"task_id：{task.id}",
        f"状态：{status_text}",
    ]
    if template is not None:
        lines.append(f"已套用模板：{template.label}（{template.key}）")
    if ref_image.strip():
        lines.append(f"参考图：{ref_image.strip()}")
    lines.append(f"实际提示词：{final_prompt}")
    lines.append("")
    lines.append(
        "请告知用户：图片正在生成，前端会自动轮询并在完成后展示。"
        "但不要替它保证一定出得来——任务随后失败的话，前端卡片会显示具体原因。"
    )
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
