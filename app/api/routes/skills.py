"""技能清单路由：GET /api/v1/skills。

前端据此渲染技能选择器。与 ``/chat/personas`` 一样，**不返回完整步骤正文**：
那是内部实现，前端只需要能展示的字段；正文只在真正使用技能时进提示词。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.chat import SkillItem, SkillList
from app.skills import skills

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillList)
async def list_skills() -> SkillList:
    items = [
        SkillItem(
            key=item.key,
            name=item.name,
            description=item.description,
            triggers=list(item.triggers),
            has_tools=item.has_tools,
            tools=[tool.name for tool in item.tools],
        )
        for item in skills()
    ]
    return SkillList(items=items, enabled=bool(items))


__all__ = ["router"]
