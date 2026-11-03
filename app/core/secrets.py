"""API key 的解析链：环境变量 > 用户配置 > 打包内置。

**为什么要有这一层**

桌面版要「装完就能用」，就得把 key 带在安装包里；但同一个包里，开发机上的
``.env`` 和用户自己的 key 都应该优先。三条来源写在三个地方，如果在
``config.py`` 里串 ``or``，会变成「谁最后写谁生效」的猜谜，而且没法回答
「现在这个 key 是哪来的」——出问题时这一条比什么都重要。

优先级：

1. **环境变量**（含 ``.env``）——开发和高级用户
2. **config.json**——用户的逃生口：内置额度用完了，自己填一个就能继续用，
   不用等新版本
3. **打包内置**——``scripts/make_bundled_keys.py`` 在出包时注入

**内置 key 不安全，这点要说清楚**

``app/core/_bundled_keys.py`` 里是 base64 + 逐字节异或。它能挡住随手翻包的
人和 ``strings | grep sk-``，挡不住任何一个会写 Python 的人——盐就在同一个
文件里，反混淆只要五行。

真正的保护只有两条，而且都在服务商后台，不在代码里：

- 给这个 key **单独设低额度**，超了就停，别和主账号共用
- 泄露了**立刻换**。用户自己填的 key 不受影响，所以换 key 不需要发新版本

把它当「防止 key 出现在源码和 git 历史里」的措施是合适的，当「加密」不是。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

CONFIG_NAME = "config.json"
# 用户目录下的备用位置：exe 装在 Program Files 时那层目录写不进去
USER_DIR_NAME = ".ocean-travel-agent"

# 值 → 来源
ENV = "env"
CONFIG = "config"
BUNDLED = "bundled"

_config: dict[str, str] = {}
_config_stamp: tuple[tuple[str, float], ...] = ()


def config_files() -> list[Path]:
    """按优先级排列的候选配置文件。项目根（exe 旁边）优先，便于绿色版。"""
    return [PROJECT_ROOT / CONFIG_NAME, Path.home() / USER_DIR_NAME / CONFIG_NAME]


def _stamp(paths: list[Path]) -> tuple[tuple[str, float], ...]:
    entries: list[tuple[str, float]] = []
    for path in paths:
        try:
            entries.append((str(path), path.stat().st_mtime))
        except OSError:
            entries.append((str(path), -1.0))
    return tuple(entries)


def _coerce(raw: object) -> str:
    """只接受标量或字符串列表。

    列表要收：``"ALLOWED_ROOTS": ["D:/a", "D:/b"]`` 在 JSON 里比
    ``"D:/a,D:/b"`` 自然得多，而环境变量那边本来就是这个逗号格式。
    """
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        return ",".join(str(item).strip() for item in raw if str(item).strip())
    if isinstance(raw, (int, float, bool)):
        return str(raw)
    return ""


def _read_one(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # 配置文件坏掉不该让整个程序起不来——退回下一层来源，并留下证据
        logger.warning("配置文件 %s 读不了（%s），已忽略", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("配置文件 %s 顶层不是对象，已忽略", path)
        return {}
    return {str(key): _coerce(value) for key, value in raw.items()}


def refresh() -> None:
    """重新读用户配置。文件没变就不重读（每次 load_settings 都会调一次）。"""
    global _config, _config_stamp
    paths = config_files()
    stamp = _stamp(paths)
    if stamp == _config_stamp:
        return
    _config_stamp = stamp

    merged: dict[str, str] = {}
    # 反着合并：后读的覆盖先读的，所以优先级低的先放进去
    for path in reversed(paths):
        for key, value in _read_one(path).items():
            if value:
                merged[key] = value
    _config = merged


def bundled() -> dict[str, str]:
    """打包时注入的那份快照。没有就返回空——开发环境本来就没有。"""
    try:
        from app.core import _bundled_keys
    except ImportError:
        return {}
    try:
        return {str(key): str(value) for key, value in _bundled_keys.keys().items()}
    except Exception:
        logger.exception("内置配置解析失败，已忽略")
        return {}


def resolve(name: str) -> tuple[str, str]:
    """返回 ``(值, 来源)``。都没有时返回 ``("", "")``。"""
    from_env = (os.getenv(name) or "").strip()
    if from_env:
        return from_env, ENV
    refresh()
    from_config = _config.get(name, "")
    if from_config:
        return from_config, CONFIG
    from_bundle = bundled().get(name, "")
    if from_bundle:
        return from_bundle, BUNDLED
    return "", ""


def value(name: str) -> str:
    return resolve(name)[0]


def source(name: str) -> str:
    """这个 key 现在从哪来：``env`` / ``config`` / ``bundled``，都没有时是空串。

    额度报错时要靠它分岔：内置 key 用完让用户填自己的，自己填的用完让他去充值。
    两句话说反了，用户会去点一个他根本没有的「控制台」。
    """
    return resolve(name)[1]


def describe(names: list[str] | tuple[str, ...]) -> dict[str, str]:
    """``{名字: 来源}``，**不含值**。给 /system 和启动日志用。

    永远不要在这里返回值：这个结果会进日志、进 HTTP 响应、进前端。
    """
    return {name: resolve(name)[1] for name in names}


def reset() -> None:
    """清缓存。测试用——线上进程改配置需要重启，这是有意的。"""
    global _config, _config_stamp
    _config = {}
    _config_stamp = ()
