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
