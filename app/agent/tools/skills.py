"""元工具：让模型自己取用技能。

与 ``discover_tools`` 同一套思路：模型手上只有「技能清单」（一行一个），
需要时调用本工具拿完整步骤。区别是 ``discover_tools`` 申请的是**工具组**，
这里加载的是**流程**——步骤、模板路径、以及随技能附带的工具清单。
"""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.metrics import metrics
from app.skills import activate_skills, catalog_block, get_skill, instructions_block

_EMPTY_HINT = (
    "当前没有任何技能。如果是要申请业务工具，请改用 discover_tools；技能目录为空时不影响正常对话。"
)


class UseSkillInput(BaseModel):
    skill: str = Field(
        default="",
        description="技能 key，如「budget-split」「packing-list」；留空只列可用技能",
    )
    task: str = Field(default="", description="你打算用这个技能做什么，一句中文")


@tool(args_schema=UseSkillInput)
def use_skill(skill: str = "", task: str = "") -> str:
    """加载一个技能的完整步骤（含模板与工具清单）。

    技能清单只给了名字和一句话；要真正按某个技能做事时，**先调用本工具**
    取完整步骤，不要凭猜测执行。技能自带的工具若这轮还没绑上，会从下一轮起可用。
    """
    key = (skill or "").strip().lower()
    if not key:
        catalog = catalog_block()
        return catalog or _EMPTY_HINT

    item = get_skill(key)
    if item is None:
        listing = catalog_block() or _EMPTY_HINT
        return f"SKILL_NOT_FOUND：没有「{key}」这个技能，不要凭空执行。\n\n{listing}"

    added = activate_skills([key])
    body = instructions_block(key)
    # 模型自主加载才算「命中」。用户命令、前端选择器在 chat.py 里另有口径，
    # 三者混在一起会把「模型真的会挑技能」这件事淹没
    metrics.inc("skill_activations_total", skill=item.key, source="model")
    head = f"已加载技能「{item.name}」（{item.key}）。"
    if item.tools:
        if added:
            tail = "本技能的工具从下一轮起可用；这轮先按上面的步骤执行，不要假装已经完成。"
        else:
            tail = "本技能的工具本轮已可用，可以按步骤直接调用。"
    else:
        tail = "本技能只有步骤，没有额外工具；需要事实数据时照常调用其它工具。"
    if task.strip():
        head += f"本次任务：{task.strip()}。"
    return f"{head}\n{tail}\n\n{body}"


__all__ = ["UseSkillInput", "use_skill"]
