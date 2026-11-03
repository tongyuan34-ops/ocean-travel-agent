# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述：onedir（不是 onefile）。

**为什么不用 onefile**

onefile 每次启动都要把整个包解到临时目录。langchain + uvicorn 这一套解包
实测要好几秒，用户双击之后先看几秒白屏；而且每次解包都会被杀软重扫一遍，
误报概率明显更高。onedir 的代价只是「产物是一个文件夹」——用 zip 分发、
解压即用，对用户没有任何区别。

**为什么 web/ 和 knowledge/ 打进 _MEIPASS 而不是 exe 旁边**

它们只读、随版本走。exe 旁边那个目录是可写的（config.json、data/、logs/、
grants.json），混在一起的话用户升级覆盖安装时会把会话数据一起冲掉。

**web/ 里的 .py 要排掉**

``web/serve.py`` 是本地开发用的静态服务，随包发出去既没用又多一个可执行
文本。运行时的 BasicAuthMiddleware 本来也会拦 ``.py``，但那是第二道锁，
不该指望它——包里就不该有。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
WEB = ROOT / "web"
KNOWLEDGE = ROOT / "knowledge"

# 静态资源：逐个文件列，顺便把 .py / __pycache__ 排掉
datas = [
    (str(path), f"web/{path.relative_to(WEB).as_posix()}")
    for path in WEB.rglob("*")
    if path.is_file() and path.suffix not in (".py", ".pyc") and "__pycache__" not in path.parts
]
datas += [
    (str(path), f"knowledge/{path.relative_to(KNOWLEDGE).as_posix()}")
    for path in KNOWLEDGE.rglob("*.md")
]

# uvicorn 按字符串动态导入 loop / protocol / lifespan 实现，静态分析看不到
hiddenimports = collect_submodules("uvicorn")

# 内置 key 是 try/except ImportError 里导入的，文件不存在时也不该让打包失败
bundled = ROOT / "app" / "core" / "_bundled_keys.py"
if bundled.exists():
    hiddenimports.append("app.core._bundled_keys")
else:
    print("=" * 70)
    print("警告：没有 app/core/_bundled_keys.py，打出来的包不内置 key。")
    print("      先跑 scripts/make_bundled_keys.py 再打包。")
    print("=" * 70)

# 这些库一个都不该进包：GUI 框架会跟 pywebview 抢后端，测试/静态检查工具
# 只是开发期依赖，pandas/scipy 是某些库的可选依赖，拖进来几十 MB 用不上
excludes = [
    "tkinter",
    "matplotlib",
    "pandas",
    "scipy",
    "notebook",
    "IPython",
    "pytest",
    "ruff",
    "mypy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "gtk",
]

a = Analysis(
    [str(ROOT / "app" / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

icon = ROOT / "packaging" / "app.ico"
version_file = ROOT / "build" / "version_info.txt"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OceanTravelAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 窗口程序：不要那个黑框。代价是没有标准流，日志改落文件（见 core/logging.py）
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon) if icon.exists() else None,
    version=str(version_file) if version_file.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OceanTravelAgent",
)
