"""桌面版入口：一个原生窗口 + 一个只监听回环的本地服务。

**为什么不是「打开浏览器」**

浏览器版要用户自己记地址、自己留着那个标签页，关掉就找不回来了；还会和
他开的二十个标签混在一起。桌面版要的是双击就有一个窗口，关掉就退出。

**为什么服务只绑 127.0.0.1**

``app/standalone.py`` 是给托管平台用的，绑 0.0.0.0 并且强制 Basic Auth。
桌面版反过来：**只有本机能连**，所以不需要密码，也不会因为没配
``WEB_AUTH_PASS`` 就在日志里刷「接口对外完全开放」的告警。
用户真想用手机访问，那是 ``serve.py`` 的事，不是这个入口的事。

**端口为什么是随机的**

固定 8000 / 8080 在用户机器上迟早撞车（开发机自己就常占着）。绑 0 让系统
分配，代价是「刚才没人用」到「uvicorn 真绑上」之间有毫秒级窗口——所以
启动失败要能重试，不能傻等 60 秒健康检查超时。

**为什么要有单实例锁**

两个实例同时开着会各写一份 ``grants.json``（读-改-写），后写的覆盖先写的：
用户刚点过「允许」的目录会莫名消失，然后下一次写入又弹一次窗。这类
「有时候要授权两次」的怪事最难查，不如从根上不让它发生。
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx

from app.core.paths import PROJECT_ROOT
from app.version import __version__

logger = logging.getLogger(__name__)

WINDOW_TITLE = "Ocean · 旅行规划"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860
WINDOW_MIN = (960, 640)

HOST = "127.0.0.1"
HEALTH_PATH = "/api/v1/health"

# 冻结形态下冷启动要解包、import 一堆库，45 秒是留足余量的上限
READY_TIMEOUT = 45.0
# 单次启动尝试的等待上限：绑端口失败时不用陪着等满 45 秒
BIND_TIMEOUT = 12.0
START_ATTEMPTS = 3

DATA_DIR = PROJECT_ROOT / "data"
PORT_FILE = DATA_DIR / "instance.json"
LOCK_FILE = DATA_DIR / ".instance.lock"
LOCK_NAME = "ocean-travel-agent-single-instance"
GUIDE_NAME = "使用说明.txt"


# ---------- 端口与健康检查 ----------


def pick_port() -> int:
    """让系统分配一个当前空闲的回环端口。

    ``bind`` 之后立刻 ``close``，所以拿到的端口只是「刚刚还空着」，
    不是预订。真正的占用发生在 uvicorn 绑定那一刻，失败由调用方重试。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout: float = READY_TIMEOUT) -> bool:
    """轮询健康检查直到返回 200。超时返回 False。

    不用固定 sleep：机器快的时候 0.2 秒就起来了，固定等 3 秒纯属白等；
    慢的时候（机械盘 + 杀软扫描）固定等又不够。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.5).status_code == 200:
                return True
        except httpx.HTTPError:
            # 端口还没监听，连接被拒是正常过程，不是错误
            pass
        time.sleep(0.15)
    return False


# ---------- 单实例 ----------


class InstanceLock:
    """同一个用户只该有一个窗口。

    Windows 用命名互斥体（进程死掉由内核回收，不会留下「删不掉的锁文件」），
    其它平台退回 ``flock``。两边都拿不到就说明已经有一个在跑。
    """

    def __init__(self, name: str = LOCK_NAME, path: Path = LOCK_FILE) -> None:
        self.name = name
        self.path = path
        self._handle: Any = None
        self._file: Any = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)  # type: ignore[attr-defined]
            self._handle = None
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

    def _acquire_windows(self) -> bool:
        import ctypes
        from ctypes import wintypes

        # mypy 按 platform="linux" 检查（见 pyproject），WinDLL / get_last_error
        # 这类 Windows 专有属性它认不出，所以这两行各要一个 ignore
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        last_error = ctypes.get_last_error  # type: ignore[attr-defined]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            logger.warning("建互斥体失败（errno=%s），跳过单实例检查", last_error())
            return True
        # 183 = ERROR_ALREADY_EXISTS：名字被占了，说明前一个实例还活着
        if last_error() == 183:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _acquire_posix(self) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._file = handle
        return True


def write_port_file(port: int) -> None:
    """记下端口，方便「再点一次图标」时知道已经有实例在跑。

    写失败不影响使用（可能装在 Program Files，那层目录不可写），
    所以这里吞掉异常，只留一条日志。
    """
    try:
        PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(
            json.dumps({"port": port, "pid": os.getpid(), "version": __version__}),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.info("端口文件写不了（%s），不影响使用", exc)


def read_port_file() -> int | None:
    try:
        raw = json.loads(PORT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    port = raw.get("port") if isinstance(raw, dict) else None
    return int(port) if isinstance(port, int) else None


# ---------- 首次运行 ----------


def bootstrap_config() -> Path | None:
    """首次运行在程序目录放一份 config.json 模板，返回写入路径。

    内置 key 的额度总会用完，而「用完怎么办」的答案必须**在用户能看见的
    地方**——写进日志等于没写。留一份空值的 config.json，用户打开就知道
    该改哪一行；空值在我们的解析链里等于「没配」，会自动落回内置 key。
    """
    from app.core import secrets
    from app.core.config import KEY_NAMES

    for path in secrets.config_files():
        if path.exists():
            return None
    target = secrets.config_files()[0]
    template = dict.fromkeys(KEY_NAMES, "")
    template["_说明"] = (
        "内置额度用完后，把 MODEL_API_KEY 换成你自己的 key 再重启即可。"
        "这里填的值优先于内置 key。"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.info("config.json 模板写不了（%s），跳过", exc)
        return None
    return target


def write_guide() -> Path | None:
    """首次运行写一份使用说明。只在冻结形态写，免得开发时在仓库里生成垃圾。"""
    if not getattr(sys, "frozen", False):
        return None
    target = PROJECT_ROOT / GUIDE_NAME
    if target.exists():
        return None
    text = f"""Ocean 旅行规划 · 使用说明

【怎么用】
双击 OceanTravelAgent.exe 打开窗口，直接说你想去哪儿就行。

【模型额度】
这个版本内置了一份低额度 key，装完就能用，不用配置。
额度用完后会提示「内置额度已用完」——那时候：
  1. 打开同目录下的 config.json
  2. 把 MODEL_API_KEY 换成你自己的 key（任何 OpenAI 兼容接口都行）
  3. 关掉窗口重新打开

你填的 key 优先于内置 key，换 key 不需要等新版本。

【文件访问】
程序不会在启动时要求访问你的文件。只有当你让它写文件、读文件时，
才会在那一次弹窗里问「是否允许访问某个目录」，允许一次之后同目录不再问。
授权记录在 grants.json，删掉它（或删掉里面那行路径）就全部收回。

【数据在哪】
data/ 目录：会话记录、上传的图片、生成的行程。
config.json：你的 key 和配置。
grants.json：你授权过的目录。

版本 {__version__}
"""
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.info("使用说明写不了（%s），跳过", exc)
        return None
    return target


# ---------- 服务与窗口 ----------


def start_server(port: int) -> tuple[Any, threading.Thread]:
    """在后台线程里跑 uvicorn，返回 (server, thread)。

    ``log_config=None``：不传的话 uvicorn 会用它自己的 logging 配置覆盖掉
    ``app/core/logging.py`` 那一套，trace_id 和格式全丢。
    ``access_log=False``：回环地址 + 桌面场景，访问日志只是噪音。
    """
    import uvicorn

    from app.standalone import build_standalone_app

    config = uvicorn.Config(
        build_standalone_app(require_auth=False),
        host=HOST,
        port=port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return server, thread


def boot(preferred: int = 0) -> tuple[Any, int, str]:
    """起服务并等到健康检查通过，返回 (server, port, url)。

    端口是系统分配的，绑的那一刻才可能失败，所以这里重试而不是一次定生死。
    ``preferred`` 只在第一次尝试时用（``--port`` 调试），重试一律换随机端口——
    指定的那个既然被占了，再试一次还是会被占。
    """
    last_error = ""
    for attempt in range(1, START_ATTEMPTS + 1):
        port = preferred if (attempt == 1 and preferred) else pick_port()
        server, thread = start_server(port)
        url = f"http://{HOST}:{port}"
        if wait_until_ready(url + HEALTH_PATH, BIND_TIMEOUT):
            return server, port, url
        # 线程已经死了 = 端口被抢或配置炸了；还活着 = 只是慢，再等等
        if not thread.is_alive():
            last_error = "服务线程已退出"
            logger.warning("第 %d 次启动失败：%s，换端口重试", attempt, last_error)
            continue
        if wait_until_ready(url + HEALTH_PATH, READY_TIMEOUT - BIND_TIMEOUT):
            return server, port, url
        last_error = "健康检查超时"
        server.should_exit = True
    raise RuntimeError(f"本地服务起不来：{last_error}")


def open_window(url: str) -> None:
    """开原生窗口，阻塞到窗口关闭。

    没装 pywebview 时退回系统浏览器——少一个依赖就整个程序打不开，
    是比「界面丑一点」严重得多的问题。
    """
    try:
        import webview
    except ImportError:
        logger.warning("没装 pywebview，改用系统浏览器打开 %s", url)
        webbrowser.open(url)
        wait_for_interrupt()
        return

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN,
    )
    webview.start()


def wait_for_interrupt() -> None:
    """没有窗口时靠 Ctrl+C 退出（``--no-window`` 调试和浏览器回退都用它）。"""
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        return


def alert(message: str) -> None:
    """起窗口之前出的错，只能靠系统弹窗说——那时候还没有界面可以显示。"""
    logger.error(message)
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, WINDOW_TITLE, 0x10)  # type: ignore[attr-defined]
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ocean-desktop", description="Ocean 旅行规划 · 桌面版")
    parser.add_argument("--no-window", action="store_true", help="只起服务不开窗口（调试用）")
    parser.add_argument("--port", type=int, default=0, help="指定端口，默认让系统分配")
    args = parser.parse_args(argv)

    # PyInstaller 打包后子进程会重新执行本模块，不调这个会无限开窗口
    multiprocessing.freeze_support()

    lock = InstanceLock()
    if not lock.acquire():
        port = read_port_file()
        where = f"\n\n已经开着的那个在 {HOST}:{port}" if port else ""
        alert(f"程序已经在运行了，请到任务栏找一下那个窗口。{where}")
        return 0

    server: Any = None
    try:
        created = bootstrap_config()
        if created:
            logger.info("已生成配置模板：%s", created)
        write_guide()

        server, port, url = boot(args.port)
        logger.info("本地服务已就绪：%s（版本 %s）", url, __version__)
        write_port_file(port)

        if args.no_window:
            wait_for_interrupt()
        else:
            open_window(url)
        return 0
    except Exception as exc:  # noqa: BLE001 - 桌面入口必须自己兜住，否则窗口都不弹
        # 窗口模式没有控制台，光说「启动失败」用户没法反馈。把日志路径带上，
        # 他截个图就能说清问题在哪
        alert(f"启动失败：{exc}\n\n详细日志：{PROJECT_ROOT / 'logs' / 'app.log'}")
        return 1
    finally:
        # 窗口关掉之后要让 uvicorn 走一遍 lifespan 关闭：会话落盘、MCP 子进程
        # 收尾都在那里。daemon 线程直接随进程死掉的话，最后几条会话会丢。
        if server is not None:
            server.should_exit = True
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
