"""用户按需授予的目录：程序自己写的状态，和手写的 config.json 分开。

**为什么不塞进 config.json**

``config.json`` 是用户手写的（key、ALLOWED_ROOTS 都在里面），``grants.json``
是程序写的。混在一个文件里，用户下次手改配置时就可能把我们写进去的内容
覆盖掉，或者反过来——一次误操作把用户的 key 弄丢。分开存，各自负责。

**为什么单独一层，而不是直接改 ALLOWED_ROOTS**

环境变量优先级最高。如果授权结果写进 ``ALLOWED_ROOTS``，而用户的 ``.env``
里本来就有这个变量，那授权等于没生效——程序记了，配置一读还是老的。
所以授权是**追加**的：``security.allowed_roots()`` = 配置里的 + 这里记的。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.core.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

GRANTS_NAME = "grants.json"
USER_DIR_NAME = ".ocean-travel-agent"

_cache: tuple[tuple[str, float], ...] = ()
_roots: list[Path] = []


def _candidates() -> list[Path]:
    """读的时候两份都看：绿色版在 exe 旁边，装在 Program Files 的只能写用户目录。"""
    return [PROJECT_ROOT / GRANTS_NAME, Path.home() / USER_DIR_NAME / GRANTS_NAME]


def _writable() -> Path:
    """挑一个真能写的位置。先试 exe 旁边（绿色版，用户找得到），不行再退用户目录。"""
    for path in _candidates():
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            probe = parent / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    # 都不行就返回用户目录：写入时还会再失败一次，但至少错误信息指向对的地方
    return _candidates()[-1]


def _stamp() -> tuple[tuple[str, float], ...]:
    entries: list[tuple[str, float]] = []
    for path in _candidates():
        try:
            entries.append((str(path), path.stat().st_mtime))
        except OSError:
            entries.append((str(path), -1.0))
    return tuple(entries)


def _read_one(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("授权记录 %s 读不了（%s），已忽略", path, exc)
        return []
    if not isinstance(raw, dict):
        return []
    roots = raw.get("roots")
    if not isinstance(roots, list):
        return []
    return [str(item) for item in roots if isinstance(item, str) and item.strip()]


def load() -> list[Path]:
    """当前已授权的目录。文件没变就复用上次解析的结果。"""
    global _cache, _roots
    stamp = _stamp()
    if stamp == _cache:
        return list(_roots)

    _cache = stamp
    seen: list[Path] = []
    for path in _candidates():
        for raw in _read_one(path):
            try:
                resolved = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if resolved not in seen:
                seen.append(resolved)
    _roots = seen
    return list(_roots)


def _write(roots: list[Path]) -> None:
    target = _writable()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"roots": [str(root) for root in roots]}, ensure_ascii=False, indent=2)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(payload + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, target)


def add(root: Path) -> bool:
    """记一条授权。已经授权过就返回 False，不重复写盘。"""
    global _cache
    try:
        resolved = root.expanduser().resolve()
    except (OSError, RuntimeError):
        return False

    current = load()
    if resolved in current:
        return False
    current.append(resolved)
    try:
        _write(current)
    except OSError as exc:
        # 记不下来不影响本次使用（内存里已经放行），但下次会再问一遍
        logger.warning("授权记录写不进去（%s），本次有效、下次会再问", exc)
        _roots.append(resolved)
        _cache = ()
        return True

    _cache = ()  # 让下一次 load() 重新读，确认真的落盘了
    load()
    logger.info("已授权目录：%s", resolved)
    return True


def revoke(root: Path) -> bool:
    """撤销一条授权。给「我后悔了」留条路。"""
    global _cache
    try:
        resolved = root.expanduser().resolve()
    except (OSError, RuntimeError):
        return False

    current = load()
    if resolved not in current:
        return False
    current.remove(resolved)
    try:
        _write(current)
    except OSError:
        return False
    _cache = ()
    load()
    logger.info("已撤销目录授权：%s", resolved)
    return True


def reset() -> None:
    """清缓存。测试用。"""
    global _cache, _roots
    _cache = ()
    _roots = []
