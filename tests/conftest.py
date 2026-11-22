"""pytest 公共 fixture。

把项目根塞进 sys.path 是必需的：pytest 默认只把 tests/ 目录加入路径，
不这么做 `import app` 会失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 用例不能写真实存储：``client`` 用的是模块级的 app，它会照 .env / 环境变量
# 去打开 data/sessions.db。不拦住的话每跑一次 pytest，真实会话库里就多出一批
# 测试产生的会话（删除、改风格也一样会落到真数据上）。
# 必须在 app.main 被 import 之前设置——settings 是建 app 时算出来的。
os.environ["STORAGE_BACKEND"] = "memory"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings, load_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_geocode_cache():
    """地名解析带进程内缓存，用例之间必须清掉。

    不清的话，上一个用例用打桩的高德客户端解析出的坐标会留在缓存里，
    下一个用例改成「解析必然失败」时仍然拿到旧结果——测出来的通过与否
    取决于用例执行顺序，这种失败最难查。
    """
    from app.agent.tools.info import _GEOCODE_CACHE

    _GEOCODE_CACHE.clear()
    yield
    _GEOCODE_CACHE.clear()


@pytest.fixture(autouse=True)
def _silence_langfuse(monkeypatch):
    """测试进程不往 Langfuse 发数据。

    开发机的 .env 里配了真的 key，不拦的话每跑一次测试就往本地实例灌一批
    trace，CI 上则是一堆连不上的报错日志——两者都跟断言无关，只会让失败变难读。
    要测追踪本身的用例自己用 monkeypatch 打开模块状态。
    """
    monkeypatch.setenv("LANGFUSE_TRACING", "false")


@pytest.fixture
def settings() -> Settings:
    """真实配置（读 .env）。"""
    return load_settings()


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """一个空的临时沙箱目录。"""
    root = tmp_path / "sandbox"
    root.mkdir()
    return root


@pytest.fixture
def client():
    """带 lifespan 的 TestClient。

    必须用 with 进入：否则 lifespan 不执行，app.state 上不会挂
    media / chat / session_store，接口会直接报 AttributeError。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
