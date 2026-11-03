"""路径沙箱、SSRF 防护与 Cookie 凭据读写。

对应 SPEC 1.3 底线二（副作用授权）与 4.7 的 SSRF 要求、
4.6 system 组的沙箱要求。
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from app.core.net import UrlNotAllowed, ensure_safe_url
from app.core.security import (
    GrantNeeded,
    PathNotAllowed,
    build_write_preview,
    cookie_path,
    delete_path,
    ensure_allowed,
    read_cookie,
    suggest_grant_root,
    write_text,
)

# ---------- 路径沙箱 ----------


def test_path_inside_root_allowed(sandbox):
    target = sandbox / "a" / "b.txt"
    assert ensure_allowed(str(target), [sandbox]) == target.resolve()


def test_parent_escape_rejected(sandbox):
    with pytest.raises(PathNotAllowed):
        ensure_allowed(str(sandbox / ".." / "outside.txt"), [sandbox])


def test_sibling_with_same_prefix_rejected(sandbox):
    """按路径层级比较，而不是字符串前缀：`sandbox-evil` 不能被 `sandbox` 放行。"""
    sibling = sandbox.parent / f"{sandbox.name}-evil"
    sibling.mkdir()
    with pytest.raises(PathNotAllowed):
        ensure_allowed(str(sibling / "x.txt"), [sandbox])


def test_empty_whitelist_rejects_everything(sandbox):
    with pytest.raises(PathNotAllowed):
        ensure_allowed(str(sandbox / "x.txt"), [])


# ---------- 按需授权：越界时该问哪个目录 ----------


def test_out_of_range_offers_a_grant_instead_of_hard_refusal(sandbox):
    """越界不等于「永远不行」：带上建议授权的目录，工具层才能问一次。"""
    with pytest.raises(GrantNeeded) as info:
        ensure_allowed(str(sandbox / "x.txt"), [])

    assert info.value.suggested_root is not None
    # 建议的目录必须真的能覆盖目标，否则用户点了允许还是写不进去
    assert str(info.value.suggested_root) in str(sandbox.resolve())


def test_suggest_grant_root_prefers_a_folder_the_user_recognises(tmp_path, monkeypatch):
    """给「桌面」而不是给文件所在的深层目录。

    授权一次要能覆盖同目录后续所有操作，用户也不用判断「授权这一层够不够」。
    """
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    target = home / "Desktop" / "旅行" / "行程.md"

    assert suggest_grant_root(target) == (home / "Desktop").resolve()


def test_suggest_grant_root_refuses_system_locations():
    """系统目录不该出现在「允许访问」按钮后面。"""
    if os.name == "nt":
        drive = os.environ.get("SYSTEMDRIVE", "C:")
        targets = [
            Path(f"{drive}\\"),
            Path(f"{drive}\\Windows\\System32\\cmd.exe"),
            Path(f"{drive}\\Program Files\\app\\x.dll"),
        ]
    else:
        targets = [Path("/"), Path("/etc/passwd"), Path("/usr/bin/env")]

    for target in targets:
        assert suggest_grant_root(target) is None, target


def test_suggest_grant_root_falls_back_to_the_parent_folder(tmp_path, monkeypatch):
    """不在桌面/文档下的自有目录，就授权它自己那一层。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    folder = tmp_path / "work" / "trips"
    folder.mkdir(parents=True)

    assert suggest_grant_root(folder / "a.md") == folder.resolve()


def test_delete_refuses_non_empty_dir(sandbox):
    child = sandbox / "sub"
    child.mkdir()
    (child / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PathNotAllowed):
        delete_path(str(child), [sandbox])


def test_delete_refuses_root_itself(sandbox):
    with pytest.raises(PathNotAllowed):
        delete_path(str(sandbox), [sandbox])


def test_delete_empty_dir_ok(sandbox):
    empty = sandbox / "empty"
    empty.mkdir()
    assert "已删除空目录" in delete_path(str(empty), [sandbox])


def test_write_preview_marks_existing_file(sandbox):
    target = sandbox / "note.md"
    write_text(str(target), "hello", [sandbox])
    preview = build_write_preview(str(target), "hello", [sandbox])
    assert preview["exists"] == "true"
    assert preview["path"].endswith("note.md")


# ---------- SSRF 防护 ----------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/health",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "file:///c:/Windows/win.ini",
        "ftp://example.com/a.txt",
        "gopher://example.com/",
        "https:///no-host",
    ],
)
async def test_ssrf_blocks(url: str) -> None:
    with pytest.raises(UrlNotAllowed):
        await ensure_safe_url(url)


async def test_public_host_is_allowed() -> None:
    """公网域名必须放行。

    注意不能改用「只放行 is_global」的白名单判据：那会把沙箱透明转发的
    RFC2544 段（198.18.0.0/15）一并挡掉，导致所有正常外网抓取失败。
    """
    try:
        resolved = await ensure_safe_url("https://www.example.com/a/b#frag")
    except Exception as exc:  # noqa: BLE001 - 无 DNS 的环境直接跳过
        pytest.skip(f"本环境无法解析公网域名：{exc}")
    assert resolved == "https://www.example.com/a/b"  # fragment 被去掉


# ---------- Cookie 凭据 ----------


def test_cookie_helpers(settings, tmp_path):
    scoped = dataclasses.replace(settings, cookie_dir=str(tmp_path))
    assert read_cookie(scoped, "douyin") == ""
    assert cookie_path(scoped, "bilibili").name == "bilibili.txt"

    (tmp_path / "douyin.txt").write_text("sessionid=abc\n", encoding="utf-8")
    assert read_cookie(scoped, "douyin") == "sessionid=abc"
