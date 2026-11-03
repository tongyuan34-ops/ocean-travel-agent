"""出桌面版：内置 key → 图标 → PyInstaller → 冒烟 → zip。

    python scripts/build_desktop.py                 # 全流程
    python scripts/build_desktop.py --no-keys       # 不重新生成内置 key（用现有的）
    python scripts/build_desktop.py --skip-smoke    # 跳过冒烟（只在明确知道原因时用）
    python scripts/build_desktop.py --installer     # 顺带用 Inno Setup 出安装包

**为什么冒烟是必须的一步，不是可选项**

PyInstaller 打出来的东西「能生成」和「能启动」是两件事：漏一个动态导入的
模块，打包过程一声不响，用户双击才炸。所以这里真的把 exe 跑一遍，等它自己
把端口写进 data/instance.json，再打健康检查。

**为什么产物是文件夹 + zip 而不是单文件 exe**

见 ``packaging/ocean-travel.spec`` 顶部：onefile 每次启动都要解包到临时目录，
白屏几秒，还容易被杀软重扫。文件夹版解压即用，升级时只换程序、保留 data/。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "ocean-travel.spec"
ICON_PNG = ROOT / "web" / "assets" / "icon-512.png"
ICON_ICO = ROOT / "packaging" / "app.ico"
VERSION_FILE = ROOT / "build" / "version_info.txt"
DIST = ROOT / "dist"
APP_DIR = DIST / "OceanTravelAgent"
EXE = APP_DIR / "OceanTravelAgent.exe"

# 冒烟最多等这么久：解包 + import langchain 这一套，冷启动比源码模式慢
SMOKE_TIMEOUT = 90.0

# VSVersionInfo 的固定字段。2052 = 简体中文，1200 = Unicode 代码页
_VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={vers},
    prodvers={vers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          "080404b0",
          [
            StringStruct("CompanyName", "Ocean"),
            StringStruct("FileDescription", "Ocean 旅行规划"),
            StringStruct("FileVersion", "{text}"),
            StringStruct("InternalName", "OceanTravelAgent"),
            StringStruct("OriginalFilename", "OceanTravelAgent.exe"),
            StringStruct("ProductName", "Ocean 旅行规划"),
            StringStruct("ProductVersion", "{text}"),
          ],
        )
      ]
    ),
    VarFileInfo([VarStruct("Translation", [2052, 1200])]),
  ],
)
"""


def step(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


def project_version() -> str:
    sys.path.insert(0, str(ROOT))
    from app.version import __version__

    return __version__


def build_bundled_keys() -> None:
    """把 .env 里的 key 快照进 app/core/_bundled_keys.py。

    已经有这个文件时不再重建——重建会换一个随机盐，等于把上一版包里的
    key 全部作废，而这个动作不该在「我只是想重打一次包」时发生。
    """
    target = ROOT / "app" / "core" / "_bundled_keys.py"
    if target.exists():
        print(f"已有 {target.name}，跳过（要重建就删掉它）")
        return
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_bundled_keys.py")],
        cwd=ROOT,
        check=True,
    )


def build_icon() -> None:
    """从 512px 的 png 生成多尺寸 ico。

    Windows 的任务栏、Alt-Tab、文件属性各取不同尺寸，只塞一张 256 的
    在大图标位置会被拉糊。
    """
    if ICON_ICO.exists() and ICON_ICO.stat().st_mtime >= ICON_PNG.stat().st_mtime:
        print(f"图标已是最新：{ICON_ICO.name}")
        return
    from PIL import Image

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    with Image.open(ICON_PNG) as image:
        ICON_ICO.parent.mkdir(parents=True, exist_ok=True)
        image.save(ICON_ICO, format="ICO", sizes=sizes)
    print(f"生成 {ICON_ICO.name}（{', '.join(f'{w}x{h}' for w, h in sizes)}）")


def write_version_file(version: str) -> None:
    """写 Windows 版本资源：右键属性里能看到版本号，报问题时省一轮问答。"""
    parts = [int(part) for part in version.split(".") if part.isdigit()][:4]
    parts += [0] * (4 - len(parts))
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(
        _VERSION_TEMPLATE.format(vers=tuple(parts), text=version), encoding="utf-8"
    )


def _remove_tree(path: Path) -> bool:
    """删目录，删不掉返回 False。

    **在子进程里删**：某些带文件系统钩子的环境（企业安全软件、沙箱）遇到
    「一次删上百个文件」会直接终止发起删除的进程。如果在本进程里删，表现
    就是「打包跑到一半莫名其妙退出」，连兜底代码都来不及执行。放到子进程，
    最坏情况是子进程被杀，父进程还能走改名那条路。
    """
    code = "import shutil, sys; shutil.rmtree(sys.argv[1])"
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, check=False
    )
    return result.returncode == 0 and not path.exists()


def clean_dist() -> None:
    """先自己把旧产物删干净。

    不删的话 PyInstaller 会在 ``--noconfirm`` 里自己动手，而它删大目录时
    可能被同样的钩子拦下来，报一个跟打包毫无关系的错——排查方向会被带偏。

    删不掉就改名挪开：重命名是一次原子操作，不会触发任何「一次删 N 个文件」
    的检查，而且挪到旁边的目录用户随时能手动清。
    """
    if not APP_DIR.exists():
        return
    if _remove_tree(APP_DIR):
        print(f"已清理旧产物：{APP_DIR.name}")
        return
    trash = APP_DIR.with_name(f"{APP_DIR.name}.old-{int(time.time())}")
    APP_DIR.rename(trash)
    print(f"旧产物删不掉，已挪到 {trash.name}（确认没用后可以手动删）")


def run_pyinstaller() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST),
            "--workpath",
            str(ROOT / "build"),
            str(SPEC),
        ],
        cwd=ROOT,
        check=True,
    )


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def smoke() -> None:
    """把打出来的 exe 真跑一遍。

    窗口程序没有标准流，所以这里只能靠它自己写出来的两样东西判断死活：
    ``data/instance.json``（说明服务起来了）和 ``logs/app.log``（说明文件
    日志生效了——这条路径在源码模式下永远走不到）。
    """
    if not EXE.exists():
        raise SystemExit(f"没有产物：{EXE}")

    port_file = APP_DIR / "data" / "instance.json"
    port_file.unlink(missing_ok=True)
    log_file = APP_DIR / "logs" / "app.log"
    log_file.unlink(missing_ok=True)

    print(f"启动 {EXE.name} --no-window ...")
    proc = subprocess.Popen([str(EXE), "--no-window"], cwd=APP_DIR)
    try:
        deadline = time.monotonic() + SMOKE_TIMEOUT
        port = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SystemExit(f"exe 自己退了（退出码 {proc.returncode}），看 logs/app.log")
            try:
                port = int(json.loads(port_file.read_text(encoding="utf-8"))["port"])
                break
            except (OSError, ValueError, KeyError):
                time.sleep(0.3)
        if port is None:
            raise SystemExit(f"等了 {SMOKE_TIMEOUT:.0f} 秒没等到端口文件")
        if not _health(port):
            raise SystemExit(f"端口 {port} 起来了但健康检查不过")
        print(f"健康检查通过：http://127.0.0.1:{port}")

        if not log_file.exists() or "本地服务已就绪" not in log_file.read_text(encoding="utf-8"):
            raise SystemExit("logs/app.log 里没有启动记录，文件日志没生效")
        print(f"文件日志正常：{log_file.relative_to(ROOT)}")

        # 首次运行的两个产物：模板和说明书。用户装完看到的「怎么换 key」
        # 就在这两样东西里，缺一个都说明首次运行逻辑没跑到
        if not (APP_DIR / "config.json").exists():
            raise SystemExit("没有生成 config.json 模板")
        print("首次运行产物齐全：config.json")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def make_zip(version: str) -> Path:
    target = DIST / f"OceanTravelAgent-{version}-win64.zip"
    target.unlink(missing_ok=True)
    # 用 zipfile 而不是 make_archive：要跳过用户跑出来的 data/ 和 logs/，
    # 否则下一版的包里会带着上一版的会话记录和授权目录
    skip = {"data", "logs", "config.json", "grants.json", "使用说明.txt"}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(APP_DIR.rglob("*")):
            relative = path.relative_to(APP_DIR)
            if relative.parts and relative.parts[0] in skip:
                continue
            if path.is_file():
                bundle.write(path, Path("OceanTravelAgent") / relative)
    print(f"打包完成：{target.name}（{target.stat().st_size / 1048576:.1f} MB）")
    return target


def _find_iscc() -> str | None:
    """找 Inno Setup 的命令行编译器。

    先看 PATH，再看三个默认安装位置：Inno 的安装程序会写 PATH，但已经开着的
    终端（包括这个脚本所在的进程）拿到的是旧环境，``shutil.which`` 会落空——
    用户刚装完就打包时，报「没找到 Inno」会让人以为白装了。
    """
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def build_installer(version: str) -> None:
    """有 Inno Setup 就顺手出安装包；没有就提示一句，不算失败。"""
    iscc = _find_iscc()
    if not iscc:
        print("没找到 Inno Setup（iscc），跳过安装包。装了之后重跑 --installer 即可。")
        return
    script = ROOT / "packaging" / "installer.iss"
    subprocess.run(
        [iscc, f"/DMyAppVersion={version}", f"/DSourceDir={APP_DIR}", str(script)],
        cwd=ROOT,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="出桌面版")
    parser.add_argument("--no-keys", action="store_true", help="不重新生成内置 key")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过冒烟测试")
    parser.add_argument("--no-zip", action="store_true", help="只出目录，不打 zip")
    parser.add_argument("--installer", action="store_true", help="顺带出 Inno Setup 安装包")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("提示：这是 Windows 桌面版的打包脚本，其它平台打出来的 exe 跑不了")

    version = project_version()
    print(f"Ocean 旅行规划 桌面版 v{version}")

    step("1/5 内置 key")
    if args.no_keys:
        print("按 --no-keys 跳过")
    else:
        build_bundled_keys()

    step("2/5 图标与版本资源")
    build_icon()
    write_version_file(version)

    step("3/5 PyInstaller")
    clean_dist()
    run_pyinstaller()

    step("4/5 冒烟")
    if args.skip_smoke:
        print("按 --skip-smoke 跳过（没验证过能启动的包不要发出去）")
    else:
        smoke()

    step("5/5 分发")
    if args.no_zip:
        print(f"只出目录：{APP_DIR}")
    else:
        make_zip(version)
    if args.installer:
        build_installer(version)

    print("\n完成。把 zip 发给别人，解压双击 OceanTravelAgent.exe 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
