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

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
WEB = ROOT / "web"
KNOWLEDGE = ROOT / "knowledge"
SKILLS = ROOT / "skills"


def data_pairs(
    source: Path,
    target: str,
    suffixes: tuple[str, ...] | None = None,
    keep_py: bool = False,
):
    """把目录下的文件映射成 PyInstaller 的 datas 条目。

    **第二个元素是目标目录，不是目标文件路径。** 写成
    ``(…/index.html, "web/index.html")`` 会得到
    ``_internal/web/index.html/index.html`` —— 每个静态文件都被塞进一个同名
    文件夹里，于是整个前端 404、知识库语料也读不到，而打包过程一声不响。

    这个坑在第一次打包时就存在，直到有人真的打开窗口才被发现。所以
    ``scripts/build_desktop.py`` 的冒烟现在会请求首页并检查 HTML。
    """
    pairs: list[tuple[str, str]] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        if suffixes is not None and path.suffix not in suffixes:
            continue
        # web/ 里混着 serve.py 这类源码，随包发出去既没用又多一个可执行文本。
        # 技能目录相反：tools.py 是运行时用 importlib 加载的，必须进包
        if "__pycache__" in path.parts:
            continue
        if not keep_py and path.suffix in (".py", ".pyc"):
            continue
        parent = path.parent.relative_to(source).as_posix()
        folder = target if parent == "." else f"{target}/{parent}"
        pairs.append((str(path), folder))
    return pairs


# 静态资源、知识库语料、技能目录：都是只读、随版本走，放进 _MEIPASS。
# 技能必须保留 .py：tools.py 是 importlib 运行时加载的，静态分析看不到它
datas = (
    data_pairs(WEB, "web")
    + data_pairs(KNOWLEDGE, "knowledge", suffixes=(".md",))
    + data_pairs(SKILLS, "skills", keep_py=True)
)

# python-docx 自带一份 default.docx 模板，``Document()`` 不带参数时要读它。
# 应用里只打开已有文档，理论上用不到，但「理论上用不到」正是上次 knowledge/
# 目录没打进包时的说法——所以这里显式收进来，代价是几十 KB。
# openpyxl 是纯 Python，没有需要额外收集的数据文件。
datas += collect_data_files("docx")

# uvicorn 按字符串动态导入 loop / protocol / lifespan 实现，静态分析看不到
hiddenimports = collect_submodules("uvicorn")

# Langfuse 是可选依赖，且只在 app/core/tracing.py 的函数体里导入（为了冷启动）。
# 静态分析一般能跟到，但 OTLP 导出器里还有按协议名动态取实现的地方——漏一个
# 就是「配了 key 却一条 trace 都没有」，很难从日志看出来。装了才收，没装不影响打包
if importlib.util.find_spec("langfuse") is not None:
    hiddenimports += [
        "langfuse",
        "langfuse.langchain",
        "langfuse.langchain.CallbackHandler",
        "opentelemetry.exporter.otlp.proto.http",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    ]
else:
    print("=" * 70)
    print("提示：环境里没装 langfuse，打出来的包不支持 Langfuse 追踪（不影响其它功能）。")
    print("      需要的话先 uv sync --extra tracing 再打包。")
    print("=" * 70)

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
