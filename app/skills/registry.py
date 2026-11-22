"""技能注册表：加载、挑选、拼提示词、绑定工具。

**技能与工具路由的关系**

内置工具按「能力组」路由（``app/agent/toolset.py``），技能的代码工具不在
任何组里——它们只在某个技能被选中或命中触发词时才绑定。这样一句「你好」
不会替四个技能付工具 schema 的钱，而用户明确说「帮我列行前清单」时，
技能工具又和普通工具一样出现在模型手上。

**模型自主 vs 用户显式**

- 用户显式：``/skill <key>``、会话上保存的技能（``ChatSession.skill``）、
  前端选择器，最终都落到同一个 key；
- 模型自主：提示词里只有「技能清单」（一行一个），模型判断需要时调用
  ``use_skill`` 取完整步骤。命中触发词的技能，工具本轮就已经绑上，
  所以拿到步骤后可以在同一轮继续调用。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextvars import ContextVar
from pathlib import Path

from langchain_core.tools import BaseTool

from app.core.paths import resolve_resource
from app.skills.loader import Skill, load_skills

_cache: dict[str, Skill] = {}
_cache_root: Path | None = None
_root_path: Path | None = None
_loaded = False

# 槽斜杠命令：``/skill <key>`` 或直接 ``/<key>``。
# 只认已存在的 key——用户顺手打出的其它斜杠文本不该被吃掉。
_COMMAND_RE = re.compile(
    r"^\s*/(?:skill\s+)?([a-z0-9][a-z0-9_-]{0,31})(?:\s+|$)(.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _root() -> Path:
    """技能根目录。

    解析一次就缓存：``load_settings()`` 每次要读 .env、config.json 并组装
    七十多个字段，实测 22ms。而 ``skills()`` 在一轮对话里会被
    ``get_skill`` / ``match_skills`` / ``catalog_block`` 反复调用，
    每次都付这 22ms 等于把事件循环按住不放。
    SKILLS_DIR 属于启动期配置，改它本来就要重启进程。
    """
    global _root_path
    if _root_path is None:
        # 延迟导入：config 会 import secrets，而 secrets 不值得在技能模块的
        # import 阶段就被拉进来（技能加载器也会被测试直接调用）
        from app.core.config import load_settings

        _root_path = resolve_resource(load_settings().skills_dir)
    return _root_path


def skills(*, refresh: bool = False) -> list[Skill]:
    """当前可用的技能，按 key 排序。

    按根目录缓存：同一进程里每轮对话都重新读盘没有必要。``refresh=True``
    给测试和「用户刚丢进一个新技能」的脚本用。
    """
    global _cache_root, _loaded
    root = _root()
    if refresh or not _loaded or _cache_root != root:
        _cache.clear()
        _cache.update(load_skills(root))
        _cache_root = root
        _loaded = True
    return sorted(_cache.values(), key=lambda item: item.key)


def reload() -> list[Skill]:
    """重新解析根目录并重新加载。测试与「用户刚丢进一个新技能」时用。"""
    global _root_path
    _root_path = None
    return skills(refresh=True)


def get_skill(key: str | None) -> Skill | None:
    text = (key or "").strip().lower()
    if not text:
        return None
    return next((item for item in skills() if item.key == text), None)


def count() -> int:
    return len(skills())


def match_skills(text: str, limit: int = 2) -> list[str]:
    """按触发词挑技能。命中数高的优先，同分按 key 排序。

    只取前 ``limit`` 个：一句话同时沾到「预算」和「清单」是正常的，
    但把四个技能的正文和工具都塞进来，预算就被摊薄了。
    """
    lowered = (text or "").lower()
    if not lowered:
        return []
    scores: list[tuple[int, str]] = []
    for item in skills():
        score = sum(1 for word in item.triggers if word and word in lowered)
        if score:
            scores.append((score, item.key))
    scores.sort(key=lambda pair: (-pair[0], pair[1]))
    return [key for _score, key in scores[:limit]]


def catalog_block() -> str:
    """系统提示词里的技能清单。只放名字和一句话，正文等用到再加载。"""
    items = skills()
    if not items:
        return ""
    lines = ["可用技能（需要时先调用 use_skill 获取完整步骤，不要凭猜测执行）："]
    for item in items:
        tools = "；含可调用工具" if item.has_tools else ""
        lines.append(f"- {item.key}｜{item.name}：{item.description}{tools}")
    lines.append('调用方式：use_skill(skill="技能 key")；本轮按返回的步骤执行。')
    return "\n".join(lines)


def instructions_block(key: str) -> str:
    """某个技能的完整步骤 + 资源路径，注入提示词或作为 use_skill 的返回。

    模板路径给的是绝对路径，但**不要求模型去读**：模板由技能工具自己套用。
    写出来是为了模型知道有哪些资源、以及生成的文件会长什么样，
    不至于把「模板」当成一句比喻。
    """
    item = get_skill(key)
    if item is None:
        return ""
    lines = [f"【当前技能：{item.name}（{item.key}）】", item.instructions]
    if item.templates:
        lines.append("技能资源（由技能工具自动套用，不要用 read_file 打开）：")
        lines.extend(f"- {path.name}：{path}" for path in item.templates)
        lines.extend(_inline_templates(item.templates))
    if item.tools:
        lines.append("技能工具：" + "、".join(tool.name for tool in item.tools))
    if item.error:
        lines.append(f"注意：这个技能的工具没能加载（{item.error}），只能按上面的步骤回答。")
    return "\n".join(lines)


# 模板正文最多内联多少字符：模板的作用是「统一表头/段落」，不是语料库。
# 超过这个长度说明它不适合直接进提示词，只保留路径，交给技能工具自己读
_INLINE_TEMPLATE_TOTAL = 1500


def _inline_templates(paths: Iterable[Path] | None) -> list[str]:
    """把小模板的正文原样带进提示词。

    纯提示词型技能没有代码去读模板，只给一个路径的话模板等于没用——
    模型既读不到（在沙箱之外），也不知道表头长什么样。短模板直接内联，
    长模板只给路径，由带代码的技能自行读取。
    """
    budget = _INLINE_TEMPLATE_TOTAL
    blocks: list[str] = []
    for path in paths or ():
        suffix = path.suffix.lower()
        if suffix not in {".md", ".txt", ".csv"}:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if not text or len(text) > budget:
            continue
        budget -= len(text)
        blocks.append(f"模板内容（{path.name}）：\n{text}")
    return blocks


def skill_tools() -> list[BaseTool]:
    return [tool for item in skills() for tool in item.tools]


def tools_for_skills(keys: Iterable[str] | None) -> list[BaseTool]:
    """按技能 key 取工具。顺序跟随技能顺序，保证图缓存键稳定。"""
    wanted = {str(key).strip().lower() for key in keys or ()}
    return [tool for item in skills() if item.key in wanted for tool in item.tools]


def skill_of_tool(name: str) -> str | None:
    for item in skills():
        if any(tool.name == name for tool in item.tools):
            return item.key
    return None


def is_skill_tool(name: str) -> bool:
    return (name or "").startswith("skill__") or skill_of_tool(name) is not None


def split_command(text: str) -> tuple[str, str]:
    """识别用户消息开头的技能命令，返回 ``(技能 key, 剩余正文)``。

    未知 key 原样返回，不吞用户输入：``/tmp 下有什么`` 这类普通斜杠文本
    应该照常交给模型。
    """
    raw = text or ""
    match = _COMMAND_RE.match(raw)
    if not match:
        return "", raw.strip()
    key = match.group(1).lower()
    if get_skill(key) is None:
        return "", raw.strip()
    return key, match.group(2).strip()


# ---------- 跨轮激活 ----------

# 与 ``toolset.bind_active`` 同一套做法：``use_skill`` 在工具执行的后台任务里
# 往这个可变集合加 key，主流程在流结束后回写会话，下一轮就能绑上它的工具。
_active_skills: ContextVar[set[str] | None] = ContextVar("skill_active", default=None)


def bind_active_skills(keys: Iterable[str] | None) -> set[str]:
    box = {str(key).strip().lower() for key in keys or ()}
    _active_skills.set(box)
    return box


def activate_skills(keys: Iterable[str] | None) -> list[str]:
    """把技能激活到当前会话。返回真正新增的 key。

    没有绑定上下文（脚本直接调 ``use_skill``、单测）时按「已激活」返回：
    那时本来就没有路由，报「没激活」只会误导调用方。
    """
    valid = [str(key).strip().lower() for key in keys or () if get_skill(str(key))]
    box = _active_skills.get()
    if box is None:
        return valid
    added = [key for key in valid if key not in box]
    box.update(added)
    return added


__all__ = [
    "activate_skills",
    "bind_active_skills",
    "catalog_block",
    "count",
    "get_skill",
    "instructions_block",
    "is_skill_tool",
    "match_skills",
    "reload",
    "skill_of_tool",
    "skill_tools",
    "skills",
    "split_command",
    "tools_for_skills",
]
