"""日志配置的测试。

重点只有一条：**没有控制台的时候不能崩**。PyInstaller 的窗口模式把
``sys.stdout`` 置成 None，``StreamHandler(None)`` 每次写日志都抛异常，
现象是「双击没反应」，而日志里一个字的线索都没有。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app.core import logging as app_logging


@pytest.fixture(autouse=True)
def _clean() -> None:
    """每条用例前后都把 root logger 恢复原状，别污染其它测试。"""
    app_logging.reset()
    yield
    app_logging.reset()


def test_uses_stream_when_console_exists() -> None:
    app_logging.configure_logging("INFO")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], RotatingFileHandler)


def test_falls_back_to_file_without_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没有标准流时写文件，而且要真的写得进去。

    这条用例盯的是打包后的形态：窗口程序里日志是唯一的现场证据，
    「文件建了但写不进去」和「没有文件」对排查是一回事。
    """
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(app_logging, "LOG_DIR", tmp_path / "logs")

    app_logging.configure_logging("INFO")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)

    logging.getLogger("app.test").info("写一条")
    target = tmp_path / "logs" / app_logging.LOG_NAME
    assert target.exists()
    assert "写一条" in target.read_text(encoding="utf-8")
    # 带 BOM：中文 Windows 的记事本 / PowerShell 5.1 默认按 GBK 读无 BOM 文件，
    # 少了这个标记，用户打开日志看到的就是乱码
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_null_handler_when_log_dir_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """日志目录写不进去（装在 Program Files）时退回空 handler。

    没有日志可以接受，每次写日志都抛异常不行——那会把正常请求也搞挂。
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("我是个文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(app_logging, "LOG_DIR", blocker / "logs")

    app_logging.configure_logging("INFO")
    assert isinstance(logging.getLogger().handlers[0], logging.NullHandler)
    # 关键：写日志不抛
    logging.getLogger("app.test").warning("不该炸")


def test_configure_is_idempotent() -> None:
    """多次调用只挂一个 handler——重复挂会让每行日志打两遍。"""
    app_logging.configure_logging("INFO")
    first = list(logging.getLogger().handlers)
    app_logging.configure_logging("INFO")
    assert logging.getLogger().handlers == first


def test_record_carries_trace_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """格式里的 [trace_id] 必须有值，没有上下文时是短横线。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(app_logging, "LOG_DIR", tmp_path / "logs")
    app_logging.configure_logging("INFO")

    logging.getLogger("app.test").info("无上下文")
    text = (tmp_path / "logs" / app_logging.LOG_NAME).read_text(encoding="utf-8")
    assert f"[{app_logging._NO_TRACE}]" in text
