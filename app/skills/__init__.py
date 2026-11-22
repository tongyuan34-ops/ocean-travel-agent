"""插件式技能：目录化的工作流（提示词 + 模板 + 可选代码工具）。"""

from app.skills.loader import Skill, load_skill, load_skills
from app.skills.registry import (
    activate_skills,
    bind_active_skills,
    catalog_block,
    count,
    get_skill,
    instructions_block,
    is_skill_tool,
    match_skills,
    reload,
    skill_of_tool,
    skill_tools,
    skills,
    split_command,
    tools_for_skills,
)

__all__ = [
    "Skill",
    "activate_skills",
    "bind_active_skills",
    "catalog_block",
    "count",
    "get_skill",
    "instructions_block",
    "is_skill_tool",
    "load_skill",
    "load_skills",
    "match_skills",
    "reload",
    "skill_of_tool",
    "skill_tools",
    "skills",
    "split_command",
    "tools_for_skills",
]
