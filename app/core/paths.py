"""根目录判定：**可写目录**与**只读资源目录**必须分开。

原来这两个常量写在 ``config.py`` 里，但 ``secrets.py`` 也要用它们找
``config.json``，而 ``config.py`` 要 import ``secrets`` ——放一起会成环。
根目录是比配置更底层的概念，单独放一层是这里唯一干净的解法。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _writable_root() -> Path:
    """可写根目录：.env、media/、data/ 都落在这里。

    打包成单文件程序后，``__file__`` 在 PyInstaller 的临时解包目录里
    （``sys._MEIPASS``），进程一退就被删掉——把用户数据写进去等于每次重启
    都丢会话、丢生成的图。所以冻结形态下改用 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _resource_root() -> Path:
    """只读资源根目录：前端静态文件这类**随程序分发**的东西。

    冻结形态下它们在 ``_MEIPASS`` 里，不能和可写目录混为一谈——
    混了就会出现「exe 旁边找不到 web/」或者「静态文件被写进临时目录」。
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return Path(__file__).resolve().parents[2]


# app/core/paths.py -> app/core -> app -> 项目根
PROJECT_ROOT = _writable_root()
RESOURCE_ROOT = _resource_root()


def resolve_resource(path: str | Path) -> Path:
    """把配置里的**只读资源**路径解析成绝对路径。

    相对路径一律按 ``RESOURCE_ROOT`` 算，不能留给 ``Path(...)`` 去跟进程 CWD：
    源码运行时 CWD 通常就是项目根，看起来没问题；但打包后双击 exe，CWD 是
    exe 所在目录，而语料在 ``_internal/`` 里——于是「文件明明打进了包」和
    「运行时一个都读不到」同时成立，检索静默返回空。绝对路径（用户自己指定
    的目录）原样返回。
    """
    raw = Path(path).expanduser()
    return raw if raw.is_absolute() else RESOURCE_ROOT / raw
