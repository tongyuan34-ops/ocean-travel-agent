"""技能加载：把 ``skills/<key>/`` 读成一个可注入提示词、可绑定工具的 Skill。

**为什么技能是目录，不是一段提示词**

一段提示词只能改「怎么说」，改不了「怎么交付」。真正能复用的流程往往还要
模板文件（统一的清单/对比表）和一小段代码（套模板、算分项、落盘）。把它们
放在同一个目录里，技能才是一个完整、可版本化的东西。

**为什么 ``tools.py`` 在进程内加载，而不是起子进程跑脚本**

桌面版是 PyInstaller 打出来的 exe，``sys.executable`` 指向程序自己，不是
Python 解释器——``python skill.py`` 这种写法在装好的机器上根本跑不起来。
技能代码因此以普通 LangChain 工具的身份在进程内加载，照旧走权限确认、
污点拦截和调用预算，不需要另开一套沙箱。

代价是「装技能 = 装可执行代码」，所以只从随程序分发的 ``skills/`` 目录加载，
不做远程下载。这一点与 MCP 服务端是同一个取舍。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# 技能 key 只允许 ASCII：它会被拼进工具名、URL 和斜杠命令，
# 中文 key 在命令行里既难输入，也会让工具名在各处转义不一致
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# 技能目录最多加载多少个。上限的意义是防止一个误放的目录把提示词撑爆
MAX_SKILLS = 32
# 描述会进系统提示词的技能清单，一行一个；超过这个长度就不再是「一句话」
MAX_DESCRIPTION = 160
# 步骤正文会整段注入提示词，给一个上限防止把上下文预算吃光
MAX_INSTRUCTIONS = 6000
# 模板只收这些后缀：二进制文件（图片、压缩包）不适合直接给模型看路径
TEMPLATE_SUFFIXES = frozenset({".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".xml"})


@dataclass(frozen=True)
class Skill:
    """一个已加载的技能。``error`` 非空表示工具没加载起来，但提示词仍可用。"""

    key: str
    name: str
    description: str
    triggers: tuple[str, ...]
    instructions: str
    path: Path
    templates: tuple[Path, ...] = ()
    tools: tuple[BaseTool, ...] = ()
    error: str = ""

    @property
    def has_tools(self) -> bool:
        return bool(self.tools)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """拆出 YAML frontmatter 与正文。

    不用正则硬抠：正文里出现 ``---`` 分隔线是很正常的 Markdown 写法，
    ``split("---", 2)`` 只认最前面的两段，后面的原样留给正文。
    """
    raw = text.lstrip("\ufeff")
    if not raw.startswith("---"):
        return {}, raw.strip()
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw.strip()
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        logger.warning("技能 frontmatter 不是合法 YAML：%s", exc)
        return {}, raw.strip()
    return (meta if isinstance(meta, dict) else {}), parts[2].strip()


def _triggers(meta: dict[str, Any]) -> tuple[str, ...]:
    """触发词支持列表或逗号分隔字符串；顺手去重、去空、转小写。"""
    raw = meta.get("triggers") or ()
    if isinstance(raw, str):
        items = re.split(r"[,，\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = []
    seen: list[str] = []
    for item in items:
        word = item.strip().lower()
        if word and word not in seen:
            seen.append(word)
    return tuple(seen)


def _templates(path: Path) -> tuple[Path, ...]:
    root = path / "templates"
    if not root.is_dir():
        return ()
    found = [
        item
        for item in sorted(root.rglob("*"))
        if item.is_file() and item.suffix.lower() in TEMPLATE_SUFFIXES
    ]
    return tuple(found)


def load_tools(path: Path, key: str) -> tuple[tuple[BaseTool, ...], str]:
    """加载 ``tools.py`` 里的 ``TOOLS``，返回 ``(工具, 错误说明)``。

    工具名统一加上 ``skill__<key>__`` 前缀：技能是用户可扩展的，不同技能的
    工具重名几乎一定会发生；前缀让它们不可能撞上内置工具，也让日志和
    SSE 事件一眼看得出这是哪个技能的代码。
    """
    module_path = path / "tools.py"
    if not module_path.is_file():
        return (), ""

    module_name = f"ocean_skill_{key}_tools"
    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError("无法为技能工具创建加载器")
        module = importlib.util.module_from_spec(spec)
        # 先登记再执行：工具模块里常用的 dataclass / 类型注解会回查 sys.modules，
        # 不登记会得到「部分初始化的模块」这类难懂的报错
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 一个技能坏了不该拖垮整个应用
        sys.modules.pop(module_name, None)
        logger.warning("技能 %s 的 tools.py 加载失败，仅保留提示词：%s", key, exc)
        return (), str(exc)

    raw = getattr(module, "TOOLS", ())
    if not isinstance(raw, (list, tuple)):
        logger.warning("技能 %s 的 TOOLS 不是列表，已忽略：%r", key, type(raw).__name__)
        return (), ""

    prefix = f"skill__{key}__"
    tools: list[BaseTool] = []
    for item in raw:
        if not isinstance(item, BaseTool):
            logger.warning("技能 %s 的 TOOLS 里混入了非工具对象，已跳过：%r", key, item)
            continue
        if not item.name.startswith(prefix):
            item.name = prefix + item.name
        tools.append(item)
    return tuple(tools), ""


def load_skill(path: Path) -> Skill | None:
    """读一个技能目录。任何一处不合法都只跳过这一个技能。"""
    key = path.name.strip().lower()
    if not KEY_RE.match(key):
        logger.warning("技能目录名不是合法 key，已跳过：%s", path)
        return None

    skill_file = path / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning("技能 %s 读不到 SKILL.md：%s", key, exc)
        return None

    meta, body = _split_frontmatter(text)
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        logger.warning("技能 %s 缺少 name 或 description，已跳过", key)
        return None
    if not body:
        logger.warning("技能 %s 的正文是空的，已跳过", key)
        return None

    tools, error = load_tools(path, key)
    return Skill(
        key=key,
        name=name,
        description=description[:MAX_DESCRIPTION],
        triggers=_triggers(meta) or (name.lower(),),
        instructions=body[:MAX_INSTRUCTIONS],
        path=path,
        templates=_templates(path),
        tools=tools,
        error=error,
    )


def load_skills(root: Path) -> dict[str, Skill]:
    """扫描技能根目录。根目录不存在时返回空字典，不报错。

    「没有技能」是合法状态：用户可能把 SKILLS_DIR 指向一个空目录，
    或者这一版的包故意不带技能。让它安静地降级，而不是启动失败。
    """
    if not root.is_dir():
        logger.info("技能目录不存在，跳过加载：%s", root)
        return {}

    found: dict[str, Skill] = {}
    for path in sorted(root.iterdir()):
        if len(found) >= MAX_SKILLS:
            logger.warning("技能数量超过 %d 个，其余已忽略", MAX_SKILLS)
            break
        if not path.is_dir() or path.name.startswith((".", "_")):
            continue
        if not (path / "SKILL.md").is_file():
            continue
        skill = load_skill(path)
        if skill is not None:
            found[skill.key] = skill
    return found


__all__ = [
    "KEY_RE",
    "MAX_DESCRIPTION",
    "MAX_INSTRUCTIONS",
    "MAX_SKILLS",
    "Skill",
    "load_skill",
    "load_skills",
    "load_tools",
]
