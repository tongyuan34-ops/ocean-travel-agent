"""pytest 公共 fixture。

把项目根塞进 sys.path 是必需的：pytest 默认只把 tests/ 目录加入路径，
不这么做 `import app` 会失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings, load_settings  # noqa: E402


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
