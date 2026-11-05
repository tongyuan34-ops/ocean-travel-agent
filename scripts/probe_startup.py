"""量「双击到能用」到底花了多久。

用户抱怨的是「点了等好久才弹出页面」，那就得把这句话拆成两段量：

1. **t_window** —— 双击到窗口出现。这段长，用户会以为没点上，再点一次，
   然后撞上单实例锁。这是最伤体验的一段。
2. **t_ready** —— 窗口出现到本地服务健康检查通过（也就是页面能加载）。

只测总时长是不够的：总时长 20 秒，可能是窗口 2 秒 + 服务 18 秒（能接受，
因为有启动画面），也可能是窗口 20 秒（不可接受）。分开量才知道该修哪。

    python scripts/probe_startup.py                  # 量源码模式
    python scripts/probe_startup.py --exe <path>     # 量打包后的 exe

窗口用 Win32 ``EnumWindows`` 找：进程自己的窗口列表在 pywebview 里要等
``start()`` 之后才填，那时候已经晚了。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TITLE_HINT = "Ocean"
POLL = 0.05
GIVE_UP = 120.0


def visible_windows() -> list[tuple[int, str]]:
    """当前所有可见且有标题的顶层窗口。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[misc]
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def wait_window(timeout: float) -> tuple[float, int] | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        for hwnd, title in visible_windows():
            if TITLE_HINT in title:
                return time.monotonic() - start, hwnd
        time.sleep(POLL)
    return None


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as resp:
            return resp.status == 200
    except OSError:
        return False


def wait_ready(instance: Path, port_from: float, timeout: float) -> float | None:
    """等到 instance.json 里的端口能应答健康检查。"""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            raw = json.loads(instance.read_text(encoding="utf-8"))
            port = int(raw["port"])
        except (OSError, ValueError, KeyError, TypeError):
            time.sleep(POLL)
            continue
        if instance.stat().st_mtime >= port_from and health_ok(port):
            return time.monotonic() - start
        time.sleep(POLL)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=None, help="量打包后的 exe；不给就量源码模式")
    parser.add_argument("--keep", action="store_true", help="测完不关窗口，留着截图")
    args = parser.parse_args()

    # 打包版的 PROJECT_ROOT 是 exe 所在目录，instance.json 也就落在那边，
    # 不能一律用仓库里的 data/，否则永远读不到端口。
    app_dir = Path(args.exe).parent if args.exe else ROOT
    instance = app_dir / "data" / "instance.json"
    # instance.json 是上一次运行留下的，先删掉，否则「早就 ready」会假阳性
    instance.unlink(missing_ok=True)
    started_at = time.time()

    if args.exe:
        cmd = [args.exe]
        cwd = str(Path(args.exe).parent)
    else:
        cmd = [sys.executable, "-m", "app.desktop"]
        cwd = str(ROOT)

    # 子进程的输出不能继承到本进程的 stdout：--keep 时子进程一直活着，
    # 管道永远不 EOF，调用方（或 shell 的管道）就会一直挂着。落盘即可查。
    log_path = ROOT / "build" / "probe-child.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    try:
        win = wait_window(GIVE_UP)
        if win is None:
            print(f"✗ {GIVE_UP:.0f}s 内没等到窗口")
            return 1
        t_window, hwnd = win

        ready = wait_ready(instance, started_at, GIVE_UP)
        if ready is None:
            print(f"✓ 窗口 {t_window:.1f}s 出现，但服务始终没就绪")
            return 1

        print(f"窗口出现   {t_window:5.1f}s")
        print(f"页面可用   {t_window + ready:5.1f}s")
        print(f"  其中等待服务 {ready:.1f}s")
        print(f"hwnd={hwnd}")
        return 0
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
