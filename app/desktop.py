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
from collections.abc import Callable
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
# 窗口底色跟页面主题同色，避免加载的那零点几秒闪一下白
WINDOW_BG = "#16263f"

# 启动画面。内联在这，不能引用 web/ 里的资源——那时候服务还没起来，
# 任何外部请求都会失败，用户看到的会是「破图 + 转圈」。
SPLASH_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Ocean</title>
<style>
  html,body{height:100%;margin:0;overflow:hidden}
  body{
    background:radial-gradient(120% 100% at 50% 0%,#1d3355 0%,#16263f 55%,#0f1b2e 100%);
    color:#e8f0fb;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
    display:flex;align-items:center;justify-content:center;
    -webkit-user-select:none;user-select:none;
  }
  .box{text-align:center;transform:translateY(-10px)}
  .wave{width:92px;height:40px;margin:0 auto 18px;opacity:.9}
  .wave path{fill:none;stroke:#8fb6e6;stroke-width:2;stroke-linecap:round;
    stroke-dasharray:120;stroke-dashoffset:120;animation:draw 1.6s ease forwards}
  @keyframes draw{to{stroke-dashoffset:0}}
  .name{font-size:15px;letter-spacing:.42em;color:#cfe0f5;margin:0 0 8px;font-weight:600}
  .sub{font-size:12.5px;color:rgba(207,224,245,.62);margin:0 0 26px}
  .bar{width:168px;height:2px;margin:0 auto;background:rgba(255,255,255,.12);
    border-radius:2px;overflow:hidden}
  .bar i{display:block;width:38%;height:100%;
    background:linear-gradient(90deg,transparent,#8fb6e6,transparent);
    animation:slide 1.25s ease-in-out infinite}
  @keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(370%)}}
  .hint{font-size:11.5px;color:rgba(207,224,245,.42);margin:22px 0 0;min-height:16px}
  .close{position:fixed;top:12px;right:14px;width:30px;height:30px;border:0;
    border-radius:8px;background:transparent;color:rgba(232,240,251,.5);
    font-size:14px;line-height:1;cursor:default;font-family:inherit}
  .close:hover{background:rgba(255,255,255,.1);color:#fff}
</style></head>
<body class="pywebview-drag-region">
<button class="close" id="x" aria-label="关闭">✕</button>
<div class="box">
  <svg class="wave" viewBox="0 0 46 20" aria-hidden="true">
    <path d="M2 13c4.6-7.6 9.6-7.6 14.2 0s9.6 7.6 14.2 0 9.6-7.6 13.4-1.4"/>
  </svg>
  <p class="name">OCEAN</p>
  <p class="sub">正在启动</p>
  <div class="bar"><i></i></div>
  <p class="hint" id="hint"></p>
</div>
<script>
  var btn = document.getElementById('x');
  // 整个 body 都是拖动区，按钮要自己截住 mousedown，否则点关闭变成拖窗口
  btn.addEventListener('mousedown', function (e) { e.stopPropagation(); });
  btn.addEventListener('click', function () {
    if (window.pywebview && window.pywebview.api) window.pywebview.api.close();
  });
  // 分两段给反馈：六秒说明「不是卡死」，二十秒说明「还没完，别急着重开」。
  // 只有一段的话，超过那个时长之后界面又变成静止的，用户照样会怀疑。
  [[6000, '第一次启动会慢一些，通常 10~20 秒'],
   [22000, '还在启动…杀毒软件可能正在扫描文件，请再等等']].forEach(function (step) {
    setTimeout(function () {
      document.getElementById('hint').textContent = step[1];
    }, step[0]);
  });
</script>
</body></html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Ocean</title>
<style>
  html,body{height:100%;margin:0}
  body{background:#16263f;color:#e8f0fb;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
    display:flex;align-items:center;justify-content:center;padding:40px;box-sizing:border-box}
  .box{max-width:560px}
  h1{font-size:17px;margin:0 0 14px;font-weight:600}
  p{font-size:13px;line-height:1.75;color:rgba(232,240,251,.72);margin:0 0 12px}
  code{display:block;margin:16px 0;padding:12px 14px;border-radius:10px;
    background:rgba(0,0,0,.28);color:#ffd9d9;font-size:12px;
    word-break:break-all;white-space:pre-wrap}
  .path{color:#9dc0ea;font-size:12px}
  /* 无边框窗口没有系统 ✕，页面必须自己提供一个，否则只能去任务管理器结束进程 */
  .close{position:fixed;top:12px;right:14px;width:30px;height:30px;border:0;
    border-radius:8px;background:transparent;color:rgba(232,240,251,.5);
    font-size:14px;line-height:1;cursor:default;font-family:inherit}
  .close:hover{background:rgba(255,255,255,.1);color:#fff}
</style></head>
<body class="pywebview-drag-region">
<button class="close" id="x" aria-label="关闭">✕</button>
<div class="box">
  <h1>启动失败</h1>
  <p>本地服务没能起来，把下面这段发给开发者就能定位：</p>
  <code>__MESSAGE__</code>
  <p class="path">完整日志：__LOG__</p>
  <p>点右上角 ✕ 关掉窗口，重新打开再试一次；如果一直失败，检查日志里最后几行。</p>
</div>
<script>
  var btn = document.getElementById('x');
  btn.addEventListener('mousedown', function (e) { e.stopPropagation(); });
  btn.addEventListener('click', function () {
    if (window.pywebview && window.pywebview.api) window.pywebview.api.close();
  });
</script>
</body></html>"""


def render_error(message: str) -> str:
    """把启动失败画在窗口里。

    窗口模式没有控制台，``alert`` 也只能给一句话；用户截个图就能把错误
    和日志路径一起发出来，比让他去翻日志现实。
    """
    from html import escape

    # 不能用 %-格式化：模板里的 CSS 全是 100%、55% 这种，会撞上格式符。
    # str.format 也不行——CSS 的 {} 同样会被当占位符。老老实实替换。
    return _ERROR_HTML.replace("__MESSAGE__", escape(message)).replace(
        "__LOG__", escape(str(PROJECT_ROOT / "logs" / "app.log"))
    )

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


# 优先用这个固定端口。**端口必须尽量稳定**：窗口里的页面是
# ``http://127.0.0.1:<port>``，而 localStorage 按「源（含端口）」隔离——
# 每次换端口，壁纸、上次的会话 id、接口地址这些设置就全部回到默认值。
# 被别的程序占了就退回系统分配的临时端口（此时只是丢一次本地设置，
# 会话本身存在后端，不受影响）。
PREFERRED_PORT = 8756


def pick_port() -> int:
    """优先返回固定端口，被占用时让系统分配一个空闲的回环端口。

    ``bind`` 之后立刻 ``close``，所以拿到的端口只是「刚刚还空着」，
    不是预订。真正的占用发生在 uvicorn 绑定那一刻，失败由调用方重试。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PREFERRED_PORT))
            return PREFERRED_PORT
        except OSError:
            # 端口被占（或权限不允许），退回临时端口
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout: float = READY_TIMEOUT) -> bool:
    """轮询健康检查直到返回 200。超时返回 False。

    不用固定 sleep：机器快的时候 0.2 秒就起来了，固定等 3 秒纯属白等；
    慢的时候（机械盘 + 杀软扫描）固定等又不够。
    """
    deadline = time.monotonic() + timeout
    # trust_env=False：本机探活绝不能走系统代理。装过代理软件（Clash、公司网关）
    # 的机器上，httpx 默认会把 127.0.0.1 也送进代理，拿回 502 然后一直重试到
    # 超时——表现就是「点了没反应，很久才出窗口」，而服务其实早就起来了。
    with httpx.Client(trust_env=False, timeout=1.5) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(url).status_code == 200:
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


class WindowChrome:
    """无边框窗口的窗口按钮。

    ``frameless=True`` 之后系统标题栏就没了，最小化 / 最大化 / 关闭只能由
    前端调用——不接这几个方法，用户会得到一个既不能拖也不能关的窗口，
    只能去任务管理器结束进程。

    窗口对象要 ``create_window`` 之后才拿得到，所以用 ``_attach`` 补挂。
    下划线开头是有意的：pywebview 只把公开方法暴露给 JS，这个不该出现在
    ``window.pywebview.api`` 里。
    """

    def __init__(self) -> None:
        self._window: Any = None
        self._maximized = False

    def _attach(self, window: Any) -> None:
        self._window = window

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> bool:
        """切换最大化，返回**切换后**的状态，前端据此换图标。

        不用读 ``window.state``：那个值在 Windows 上是异步回填的，
        连点两下会拿到过期结果，图标就跟窗口对不上。
        """
        if self._window is None:
            return False
        if self._maximized:
            self._window.restore()
        else:
            self._window.maximize()
        self._maximized = not self._maximized
        return self._maximized

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()


def open_window(boot_server: Callable[[], tuple[Any, int, str]]) -> None:
    """先开窗口摆出启动画面，服务起来之后再换成真正的页面。

    **为什么不等服务就绪再开窗口**

    冻结形态下 ``import uvicorn`` + ``app.main`` 要拖进 fastapi、langchain
    一大串东西，实测 15~20 秒。这段时间里用户双击完看到的是「什么都没发生」，
    很容易以为没点开，然后再点一次——第二次会撞上单实例锁，弹一句「已经在运行了」，
    体验上等于「这软件打不开」。

    改成窗口先出来、启动画面先转，等待就从「怀疑没点上」变成「看得见在启动」。
    服务就绪后 ``load_url`` 把页面换进来，中间那一跳用户基本察觉不到。
    """
    try:
        import webview
    except ImportError:
        # 少一个依赖就整个程序打不开，比「界面丑一点」严重得多，退回浏览器
        logger.warning("没装 pywebview，改用系统浏览器打开")
        server, port, url = boot_server()
        write_port_file(port)
        webbrowser.open(url)
        try:
            wait_for_interrupt()
        finally:
            server.should_exit = True
        return

    chrome = WindowChrome()
    # 服务对象要活到窗口关闭，所以放在可变容器里而不是闭包变量
    state: dict[str, Any] = {"server": None}

    window = webview.create_window(
        WINDOW_TITLE,
        html=SPLASH_HTML,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN,
        # 无边框：去掉系统标题栏，窗口按钮由页面里的顶栏提供
        frameless=True,
        # 整窗可拖会让「选中一段文字」变成「拖走窗口」，只在顶栏拖
        easy_drag=False,
        # 无边框窗口本身没有边框阴影，靠 DWM 补一层，否则贴在桌面上像张图
        shadow=True,
        background_color=WINDOW_BG,
        js_api=chrome,
    )
    chrome._attach(window)

    # create_window 的类型是 Window | None；真失败会抛异常，这里只是让类型检查
    # 和「万一返回 None」都有明确行为，别在后面 load_url 时才炸
    if window is None:
        raise RuntimeError("窗口创建失败")

    # 起服务和初始化 WebView2 是两件互不相干的事，让它们并行。
    # 串行的话，窗口那 2 秒多的初始化就白等了——先点着火再装引擎。
    boot_result: dict[str, Any] = {}
    boot_done = threading.Event()

    def run_boot() -> None:
        try:
            boot_result["value"] = boot_server()
        except Exception as exc:  # noqa: BLE001 - 交给 worker 统一处理，这里只负责记录
            boot_result["error"] = exc
        finally:
            boot_done.set()

    threading.Thread(target=run_boot, name="boot", daemon=True).start()

    def worker() -> None:
        """等后台的 boot 完成，然后把启动画面换成真页面。

        重活（import 一大堆库、建索引、绑端口）都在 ``run_boot`` 那个线程里，
        这里只负责等待和换页，不能阻塞 GUI。
        """
        boot_done.wait()
        if "error" in boot_result:
            exc = boot_result["error"]
            logger.error("启动失败", exc_info=exc)
            window.load_html(render_error(str(exc)))
            return
        server, port, url = boot_result["value"]
        state["server"] = server
        write_port_file(port)
        logger.info("本地服务已就绪：%s（版本 %s）", url, __version__)
        window.load_url(url)

    # private_mode 默认是 True：WebView2 用内存配置档，窗口一关 localStorage
    # 全部清空——会话列表、上次的会话 id、壁纸都回到默认值，用户看到的就是
    # 「关掉再打开，历史没了」。存到 exe 旁边的 data/webview，卸载时一起清掉。
    profile_dir = PROJECT_ROOT / "data" / "webview"
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 建不出来就退回私有模式：丢本地设置总比启动失败强
        logger.warning("无法创建浏览器配置目录 %s：%s", profile_dir, exc)
        webview.start(worker)
    else:
        webview.start(worker, private_mode=False, storage_path=str(profile_dir))

    server = state["server"]
    if server is not None:
        server.should_exit = True



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


def focus_existing_window() -> bool:
    """把已经开着的那个窗口提到前台，成功返回 True。

    **为什么要这个**

    「点了没反应」的下一个动作永远是「再点一次」。如果第二次只弹一句
    「程序已经在运行了」，用户看到的是一个错误提示，而不是他刚才那个
    还在转圈的窗口——他会以为第一次真的失败了。

    提到前台就不一样了：启动画面还在转，他立刻明白「哦，在启动」。

    只有 Windows 能这么干；其它平台返回 False，走原来的提示。
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == WINDOW_TITLE:
            handles.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    if not handles:
        return False
    hwnd = handles[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # 前台锁：刚被用户点开的进程一般有权限，失败也不算错，任务栏会闪一下
    return bool(user32.SetForegroundWindow(hwnd))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ocean-desktop", description="Ocean 旅行规划 · 桌面版")
    parser.add_argument("--no-window", action="store_true", help="只起服务不开窗口（调试用）")
    parser.add_argument("--port", type=int, default=0, help="指定端口，默认让系统分配")
    args = parser.parse_args(argv)

    # PyInstaller 打包后子进程会重新执行本模块，不调这个会无限开窗口
    multiprocessing.freeze_support()

    lock = InstanceLock()
    if not lock.acquire():
        # 先试着把那个窗口提到前台——多半它正停在启动画面上转圈
        if focus_existing_window():
            return 0
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

        if args.no_window:
            # 调试用：只起服务，不开窗口，Ctrl+C 退出
            server, port, url = boot(args.port)
            logger.info("本地服务已就绪：%s（版本 %s）", url, __version__)
            write_port_file(port)
            wait_for_interrupt()
        else:
            # 窗口先出来，服务在窗口内部的后台线程里起。这里**不能**先 boot()：
            # 那等于把 15~20 秒的 import 挡在窗口前面，用户双击完看不到任何反应。
            open_window(lambda: boot(args.port))
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
