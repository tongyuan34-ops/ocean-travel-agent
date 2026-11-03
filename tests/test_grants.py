"""按需授权的持久化：用户点过「允许」之后要记得住，也要能撤销。

存的是目录，不是文件——按文件存的话，每写一个新文件就要问一次，
用户很快就会学会无脑点「允许」，那等于没有确认。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import grants


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(grants, "PROJECT_ROOT", tmp_path / "app")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    grants.reset()
    yield
    grants.reset()


def test_grant_survives_a_restart(tmp_path: Path):
    """用户点过一次「允许」，下次开程序不该再问。"""
    folder = tmp_path / "Desktop"
    folder.mkdir()

    assert grants.add(folder) is True
    grants.reset()  # 模拟重启：内存缓存清空，只剩文件

    assert grants.load() == [folder.resolve()]


def test_adding_twice_writes_once(tmp_path: Path):
    """重复授权不该把文件写两遍，也不该在列表里出现两次。"""
    folder = tmp_path / "Desktop"
    folder.mkdir()

    assert grants.add(folder) is True
    assert grants.add(folder) is False
    assert grants.load() == [folder.resolve()]

    payload = json.loads((tmp_path / "app" / grants.GRANTS_NAME).read_text(encoding="utf-8"))
    assert payload["roots"] == [str(folder.resolve())]


def test_revoke_removes_the_grant(tmp_path: Path):
    """「我后悔了」要有一条路。"""
    folder = tmp_path / "Desktop"
    folder.mkdir()
    grants.add(folder)

    assert grants.revoke(folder) is True
    grants.reset()

    assert grants.load() == []


def test_revoking_unknown_folder_is_a_no_op(tmp_path: Path):
    assert grants.revoke(tmp_path / "never-granted") is False


def test_broken_file_does_not_break_startup(tmp_path: Path):
    """授权记录坏了（断电写了一半）最多是「下次再问一次」，不该起不来。"""
    target = tmp_path / "app" / grants.GRANTS_NAME
    target.parent.mkdir(parents=True)
    target.write_text("{ 这不是 json", encoding="utf-8")

    assert grants.load() == []


def test_non_list_roots_is_ignored(tmp_path: Path):
    target = tmp_path / "app" / grants.GRANTS_NAME
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"roots": "C:\\Users"}), encoding="utf-8")

    assert grants.load() == []


def test_home_file_is_used_when_project_dir_is_unwritable(tmp_path: Path, monkeypatch):
    """装在 Program Files 时 exe 旁边写不进去，退回用户目录。"""
    blocked = tmp_path / "app"
    blocked.write_text("我是个文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(grants, "PROJECT_ROOT", blocked)

    folder = tmp_path / "Desktop"
    folder.mkdir()
    assert grants.add(folder) is True
    grants.reset()

    home_file = tmp_path / "home" / grants.USER_DIR_NAME / grants.GRANTS_NAME
    assert home_file.is_file()
    assert grants.load() == [folder.resolve()]


def test_allowed_roots_merges_config_and_grants(tmp_path: Path):
    """配置里的白名单 + 授权过的目录，两边都要生效，不能互相覆盖。

    这一条是这次设计的核心：如果授权结果写进 ``ALLOWED_ROOTS``，而用户的
    .env 里本来就有这个变量（开发机上是 ``C:\\``），那授权等于没生效。
    """
    import dataclasses

    from app.core.config import load_settings
    from app.core.security import allowed_roots

    configured = tmp_path / "configured"
    configured.mkdir()
    granted = tmp_path / "Desktop"
    granted.mkdir()
    grants.add(granted)

    settings = dataclasses.replace(load_settings(), allowed_roots=[str(configured)])
    roots = allowed_roots(settings)

    assert configured.resolve() in roots
    assert granted.resolve() in roots
