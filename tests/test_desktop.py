"""桌面入口的测试。

能测的部分都在「不弹窗」这一侧：端口分配、健康检查等待、单实例锁、
首次运行生成的配置模板、以及「桌面版不挂 Basic Auth」这个刻意选择。
真开一个窗口没法在 CI 里断言，那部分靠 ``--no-window`` 冒烟。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app import desktop


class _Ok(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        """默认实现往 stderr 打日志，测试里不要这些噪音。"""


def _serve_once() -> tuple[HTTPServer, int]:
    server = HTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_port


def test_pick_port_is_actually_free() -> None:
    """分配出来的端口要真的能绑。

    这条用例的价值在于抓「绑 0 拿端口但没关 socket」这类写法——那种实现
    在 Linux 上看着正常（SO_REUSEADDR），在 Windows 上会直接绑不上。
    """
    port = desktop.pick_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_wait_until_ready_accepts_live_server() -> None:
    server, port = _serve_once()
    try:
        assert desktop.wait_until_ready(f"http://127.0.0.1:{port}/", timeout=5.0) is True
    finally:
        server.shutdown()


def test_wait_until_ready_gives_up_instead_of_hanging() -> None:
    """没人监听的端口要**尽快**返回 False。

    端口随机分配之后，「绑的那一刻被抢」是真实存在的；如果这里退化成
    一次长连接等待，用户看到的就是双击后几十秒没有任何反应。
    """
    port = desktop.pick_port()  # 拿到就放掉，几乎一定没人监听
    started = time.monotonic()
    assert desktop.wait_until_ready(f"http://127.0.0.1:{port}/", timeout=0.6) is False
    assert time.monotonic() - started < 5.0


def test_single_instance_lock_blocks_second_copy() -> None:
    """第二个实例必须拿不到锁。

    两个实例各写一份 grants.json（读-改-写）会互相覆盖，用户刚点过
    「允许」的目录会莫名消失，然后下一次写入又弹一次窗。
    """
    first = desktop.InstanceLock(name="ocean-test-lock", path=Path("unused"))
    second = desktop.InstanceLock(name="ocean-test-lock", path=Path("unused"))
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    # 放开之后应该还能再拿到，否则程序关掉一次就再也起不来
    third = desktop.InstanceLock(name="ocean-test-lock", path=Path("unused"))
    assert third.acquire() is True
    third.release()


def test_port_file_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop, "PORT_FILE", tmp_path / "data" / "instance.json")
    desktop.write_port_file(65000)
    assert desktop.read_port_file() == 65000


def test_port_file_garbage_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端口文件被写坏不该让程序起不来——它只是「上次开在哪个端口」的备忘。"""
    target = tmp_path / "instance.json"
    monkeypatch.setattr(desktop, "PORT_FILE", target)
    target.write_text("这不是 json", encoding="utf-8")
    assert desktop.read_port_file() is None
    target.write_text('{"port": "abc"}', encoding="utf-8")
    assert desktop.read_port_file() is None


def test_bootstrap_writes_template_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首次运行放一份空值模板，让用户知道该改哪一行。

    空值在解析链里等于「没配」，所以模板本身不影响内置 key 生效——
    这一点很关键，否则用户装完就打不开。
    """
    from app.core import secrets
    from app.core.config import KEY_NAMES

    target = tmp_path / "config.json"
    monkeypatch.setattr(secrets, "config_files", lambda: [target, tmp_path / "home.json"])

    written = desktop.bootstrap_config()
    assert written == target
    data = json.loads(target.read_text(encoding="utf-8"))
    assert all(data[name] == "" for name in KEY_NAMES)
    assert "MODEL_API_KEY" in data["_说明"]

    # 第二次不该覆盖：用户可能已经填了自己的 key
    assert desktop.bootstrap_config() is None


def test_bootstrap_leaves_existing_config_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import secrets

    existing = tmp_path / "home.json"
    existing.write_text('{"MODEL_API_KEY": "sk-mine"}', encoding="utf-8")
    monkeypatch.setattr(secrets, "config_files", lambda: [tmp_path / "config.json", existing])

    assert desktop.bootstrap_config() is None
    assert json.loads(existing.read_text(encoding="utf-8"))["MODEL_API_KEY"] == "sk-mine"
    assert not (tmp_path / "config.json").exists()


def test_guide_only_written_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """开发环境不该在仓库里生成「使用说明.txt」。"""
    monkeypatch.setattr(desktop, "PROJECT_ROOT", tmp_path)
    monkeypatch.delattr(desktop.sys, "frozen", raising=False)
    assert desktop.write_guide() is None
    assert not (tmp_path / desktop.GUIDE_NAME).exists()

    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    written = desktop.write_guide()
    assert written is not None
    text = written.read_text(encoding="utf-8")
    # 说明书必须回答「内置额度用完了怎么办」，否则用户只能卸载
    assert "config.json" in text and "MODEL_API_KEY" in text


def test_desktop_app_skips_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """桌面版不挂 Basic Auth：服务只监听 127.0.0.1。

    配了 ``WEB_AUTH_PASS`` 也不能生效——嵌在窗口里的浏览器没法先弹一次
    登录框，用户会看到一个永远打不开的页面。
    """
    from fastapi.testclient import TestClient

    from app.standalone import build_standalone_app

    monkeypatch.setenv("WEB_AUTH_PASS", "pw")
    monkeypatch.setenv("STORAGE_BACKEND", "memory")

    with TestClient(build_standalone_app(require_auth=False)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


def test_second_launch_does_not_start_a_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到锁就直接退出，不建服务、不开窗口。"""
    calls: list[str] = []

    class Busy:
        def acquire(self) -> bool:
            return False

        def release(self) -> None:
            calls.append("release")

    monkeypatch.setattr(desktop, "InstanceLock", Busy)
    # 假装找不到那个窗口，走到「弹提示」这条兜底路径
    monkeypatch.setattr(desktop, "focus_existing_window", lambda: False)
    monkeypatch.setattr(desktop, "alert", lambda message: calls.append(message))
    monkeypatch.setattr(desktop, "boot", lambda: pytest.fail("不该起服务"))
    monkeypatch.setattr(desktop.multiprocessing, "freeze_support", lambda: None)

    assert desktop.main(["--no-window"]) == 0
    assert any("已经在运行" in item for item in calls)


def test_second_launch_focuses_existing_window_instead_of_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """能找到旧窗口就把它提到前台，不弹「已经在运行了」。

    弹提示等于告诉用户「你刚才那下没成功」；而实际上那个窗口就开着，
    可能正停在启动画面上转圈。用户再点一次图的是「让它出来」，不是听解释。
    """
    calls: list[str] = []

    class Busy:
        def acquire(self) -> bool:
            return False

        def release(self) -> None:
            calls.append("release")

    monkeypatch.setattr(desktop, "InstanceLock", Busy)
    monkeypatch.setattr(desktop, "focus_existing_window", lambda: True)
    monkeypatch.setattr(desktop, "alert", lambda message: calls.append(message))
    monkeypatch.setattr(desktop, "boot", lambda: pytest.fail("不该起服务"))
    monkeypatch.setattr(desktop.multiprocessing, "freeze_support", lambda: None)

    assert desktop.main([]) == 0
    assert calls == []


def test_main_hands_boot_to_window_instead_of_booting_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窗口模式**不能**在开窗口之前 ``boot()``。

    先 boot 等于把十几秒的 import 挡在窗口前面——用户双击完看不到任何反应，
    很容易以为没点开，再点一次就撞上单实例锁，弹一句「已经在运行了」。
    这条用例锁住「main 只把启动函数交给窗口，自己不抢先起服务」。
    """
    booted: list[str] = []

    class Server:
        should_exit = False

    class Free:
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            return None

    def fake_boot(preferred: int = 0) -> tuple[Server, int, str]:
        booted.append("boot")
        return Server(), 12345, "http://127.0.0.1:12345"

    handed: list[object] = []

    monkeypatch.setattr(desktop, "InstanceLock", Free)
    monkeypatch.setattr(desktop, "bootstrap_config", lambda: None)
    monkeypatch.setattr(desktop, "write_guide", lambda: None)
    monkeypatch.setattr(desktop, "write_port_file", lambda port: None)
    monkeypatch.setattr(desktop, "boot", fake_boot)
    monkeypatch.setattr(desktop, "open_window", lambda boot_server: handed.append(boot_server))
    monkeypatch.setattr(desktop.multiprocessing, "freeze_support", lambda: None)

    assert desktop.main([]) == 0
    assert handed and callable(handed[0])
    # 关键断言：main 自己一次都没起服务
    assert booted == []


def test_boot_honours_requested_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--port`` 指定了就用它（调试时要能对着固定端口开 devtools）。"""

    class Server:
        should_exit = False

    seen: list[int] = []

    def fake_start(port: int):
        seen.append(port)
        return Server(), None

    monkeypatch.setattr(desktop, "start_server", fake_start)
    monkeypatch.setattr(desktop, "wait_until_ready", lambda url, timeout: True)

    _, port, url = desktop.boot(12345)
    assert seen == [12345]
    assert port == 12345
    assert url == "http://127.0.0.1:12345"


class _FakeWindow:
    """够用的假窗口：只记下被调了哪个动作。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.loaded_url = ""
        self.loaded_html = ""

    def minimize(self) -> None:
        self.calls.append("minimize")

    def maximize(self) -> None:
        self.calls.append("maximize")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")

    def load_url(self, url: str) -> None:
        self.calls.append("load_url")
        self.loaded_url = url

    def load_html(self, html: str) -> None:
        self.calls.append("load_html")
        self.loaded_html = html


def test_window_chrome_toggles_between_maximize_and_restore() -> None:
    """按钮要来回切，而且返回值必须反映**切换后**的状态。

    前端靠这个返回值换图标。如果返回的是「切换前」，用户点两下会发现
    图标和窗口对不上，然后开始怀疑按钮坏了。
    """
    chrome = desktop.WindowChrome()
    window = _FakeWindow()
    chrome._attach(window)

    assert chrome.toggle_maximize() is True
    assert chrome.toggle_maximize() is False
    assert window.calls == ["maximize", "restore"]


def test_window_chrome_is_harmless_before_attach() -> None:
    """窗口还没挂上时调用不能崩——构造和 create_window 之间有个时间窗。"""
    chrome = desktop.WindowChrome()
    assert chrome.toggle_maximize() is False
    chrome.minimize()
    chrome.close()


def test_window_chrome_hides_attach_from_javascript() -> None:
    """``_attach`` 下划线开头，pywebview 不会把它暴露给前端。

    这条用例锁住命名约定：改成公开方法，页面里就多出一个能乱改窗口的入口。
    """
    chrome = desktop.WindowChrome()
    public = [name for name in dir(chrome) if not name.startswith("_")]
    assert sorted(public) == ["close", "minimize", "toggle_maximize"]


def test_open_window_shows_splash_then_loads_page_and_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窗口先出来、服务后起、关窗后让 uvicorn 走 lifespan。

    三件事一条用例锁住：
    1. 创建窗口时给的是**内联启动画面**，不是页面 URL（页面那时还不存在）
    2. 服务就绪后调 ``load_url`` 换成真页面
    3. ``webview.start`` 返回（窗口关掉）之后置 ``should_exit``——会话落盘、
       MCP 子进程收尾都在 lifespan 里，daemon 线程直接死掉会丢最后几条会话
    """
    import sys
    import types

    seen: dict[str, object] = {}
    fake = types.ModuleType("webview")

    class Server:
        should_exit = False

    server = Server()

    def create_window(title: str, **kwargs: object) -> _FakeWindow:
        seen["title"] = title
        seen.update(kwargs)
        return _FakeWindow()

    def start(worker=None):
        seen["started"] = True
        assert callable(worker)
        worker()  # 真的跑一遍后台任务

    fake.create_window = create_window  # type: ignore[attr-defined]
    fake.start = start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "write_port_file", lambda port: None)

    def fake_boot() -> tuple[Server, int, str]:
        return server, 12345, "http://127.0.0.1:12345"

    desktop.open_window(fake_boot)

    assert seen["started"] is True
    assert seen["frameless"] is True
    # 整窗可拖会把「选中文字」变成「拖走窗口」
    assert seen["easy_drag"] is False
    assert isinstance(seen["js_api"], desktop.WindowChrome)
    # 初始内容是启动画面，不是页面
    assert "html" in seen
    assert "正在启动" in str(seen["html"])
    assert "url" not in seen
    # 服务起来之后换页，关窗之后让 uvicorn 收尾
    assert server.should_exit is True


def test_open_window_reports_boot_failure_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务起不来时不能只是窗口卡在启动画面。

    窗口模式没有控制台，用户唯一能反馈的就是截图——所以错误要画在窗口里，
    并且带上日志路径。
    """
    import sys
    import types

    fake = types.ModuleType("webview")

    class FakeWindow:
        def __init__(self) -> None:
            self.loaded_html = ""
            self.loaded_url = ""

        def load_url(self, url: str) -> None:
            self.loaded_url = url

        def load_html(self, html: str) -> None:
            self.loaded_html = html

    holder: list[FakeWindow] = []

    def create_window(title: str, **kwargs: object) -> FakeWindow:
        window = FakeWindow()
        holder.append(window)
        return window

    fake.create_window = create_window  # type: ignore[attr-defined]
    fake.start = lambda worker=None: worker()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "webview", fake)

    def boom() -> tuple[object, int, str]:
        raise RuntimeError("端口被占")

    desktop.open_window(boom)

    assert holder[0].loaded_url == ""
    assert "启动失败" in holder[0].loaded_html
    assert "端口被占" in holder[0].loaded_html
    assert "app.log" in holder[0].loaded_html

