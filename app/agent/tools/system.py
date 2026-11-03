"""system 组工具：文件沙箱操作与桌面壁纸切换。

**目录授权是按需要的，不是启动时要的**

用户不会在装完软件的第二天就去勾选「允许访问哪些目录」——他那天还没想过
要写文件。所以这里不在启动时预授权，而是等某个操作真的要用到某个目录时，
**在那一次确认里**顺带问一句「允许访问桌面吗」。用户同意了就记进
``grants.json``，之后这个目录不再问。

写操作本来就要确认，所以「授权」和「确认」合并成**一次**询问：先问
「允许访问桌面吗」再问「确认写入这个文件吗」，第二问的答案在一秒前
已经给过了。

读操作平时不确认（SPEC 契约 2.4 的清单里没有读），只在目录还没授权时
问一次。

系统目录（``C:\\Windows``、``/etc`` 这类）连问都不问，直接拒绝——
用户点「允许」也不该把系统目录打开。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.guard import check_dangerous
from app.agent.permissions import PermissionDenied, require_permission
from app.core import grants
from app.core.config import Settings, load_settings
from app.core.context import truncate_tool_output
from app.core.security import (
    GrantNeeded,
    PathNotAllowed,
    allowed_roots,
    describe_write,
    ensure_allowed,
    is_windows,
    read_text,
    write_text,
)
from app.core.security import (
    create_dir as sandbox_create_dir,
)
from app.core.security import (
    delete_path as sandbox_delete_path,
)
from app.core.security import (
    list_dir as sandbox_list_dir,
)

# 这些扩展名按纯文本处理，打开后可以直接把内容交给模型判断
_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".csv",
    ".tsv",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".yml",
    ".yaml",
    ".xml",
    ".ini",
    ".toml",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
}

settings_cache: Settings | None = None


def _s() -> Settings:
    global settings_cache
    if settings_cache is None:
        settings_cache = load_settings()
    return settings_cache


def _roots():
    return allowed_roots(_s())


def _failed(message: str, code: str) -> str:
    return f"{code}：{message}"


# ---------- 按需授权 ----------


def _pending_grant(path: str) -> Path | None:
    """路径还没授权时返回「建议授权的目录」，已授权返回 None。

    系统目录这类硬拒绝会原样抛出 ``GrantNeeded``（``suggested_root`` 为 None），
    工具层据此直接报错——不给「允许」这个选项。
    """
    try:
        ensure_allowed(path, _roots())
    except GrantNeeded as exc:
        if exc.suggested_root is None:
            raise
        return exc.suggested_root
    return None


def _grant_detail(root: Path, path: str) -> dict[str, str]:
    return {
        "grant_root": str(root),
        "grant_hint": (
            f"这次操作要访问「{root.name or root}」（{root}），该目录还没有授权。"
            "允许之后，这个目录下的操作不再逐次询问。"
        ),
        "path": path,
    }


def _blocked_by_taint() -> str:
    """扩大授权范围也算危险动作：读到被注入的内容后不该再问「允许访问 .ssh 吗」。"""
    return check_dangerous("grant_dir")


async def _ask_grant(path: str) -> None:
    """只为「授权目录」问一次。读操作用这个——它们平时不弹确认。"""
    root = _pending_grant(path)
    if root is None:
        return
    blocked = _blocked_by_taint()
    if blocked:
        raise PathNotAllowed(blocked)
    await require_permission(
        "grant_dir",
        {
            **_grant_detail(root, path),
            "preview": "允许后仅能访问该目录及其子目录，可在 grants.json 里撤销",
        },
        remember_key=f"grant:{root}",
    )
    grants.add(root)


async def _confirm_with_grant(
    path: str,
    *,
    action: str,
    detail: dict[str, str],
    remember_key: str,
) -> None:
    """写操作：把「授权目录」并进操作确认里，一次问清。

    不合并的话就是连问两遍——用户刚点过「允许访问桌面」，紧接着又被问
    「确认写入桌面上的这个文件吗」，第二问等于噪音。
    """
    root = _pending_grant(path)
    if root is not None:
        blocked = _blocked_by_taint()
        if blocked:
            raise PathNotAllowed(blocked)
    merged = {**detail, **_grant_detail(root, path)} if root is not None else detail
    await require_permission(action, merged, remember_key=remember_key)
    if root is not None:
        grants.add(root)



# ---------- 读操作（无需确认） ----------


class PathInput(BaseModel):
    path: str = Field(
        description="目标路径（绝对路径或含 ~ 的路径），必须在 ALLOWED_ROOTS 白名单内"
    )


@tool(args_schema=PathInput)
async def list_dir(path: str) -> str:
    """列出白名单目录内的文件与子目录。只读取，不修改任何内容。"""
    try:
        await _ask_grant(path)
        items = sandbox_list_dir(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    lines = [f"{path} 共 {len(items)} 项（数据来源：本地文件系统）："]
    for item in items:
        suffix = f"（{item['size']}）" if item["kind"] == "file" and item["size"] else ""
        lines.append(f"- [{item['kind']}] {item['name']}{suffix}")
    return truncate_tool_output("\n".join(lines))


@tool(args_schema=PathInput)
async def read_file(path: str) -> str:
    """读取白名单内的文本文件。只读取，不修改任何内容。"""
    try:
        await _ask_grant(path)
        content, truncated = read_text(path, _roots())
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    note = "\n…（文件过大已截断）" if truncated else ""
    return truncate_tool_output(f"{path} 的内容：\n{content}{note}")


@tool(args_schema=PathInput)
async def open_file(path: str) -> str:
    """用系统默认程序打开白名单内文件，并读取其文本内容供判断。

    打开属于有副作用的操作，执行前会请用户确认；打开后立即回读内容，
    之后用户在外部程序里修改并保存，重新调用 read_file 即可拿到最新版本。
    """
    try:
        # 先看是不是硬拒绝（系统目录），再解析——不要先弹框再拒绝
        _pending_grant(path)
        target = Path(path).expanduser().resolve()
    except (PathNotAllowed, OSError, RuntimeError) as exc:
        message = getattr(exc, "message", str(exc))
        return _failed(message, getattr(exc, "code", "PATH_NOT_ALLOWED"))

    if not target.is_file():
        return _failed(f"不是文件：{target}", "PATH_NOT_ALLOWED")

    try:
        await _confirm_with_grant(
            path,
            action="open_file",
            detail={
                "path": str(target),
                "preview": "将用系统默认程序打开该文件，并把可读文本内容返回给助手判断",
            },
            remember_key=f"open_file:{target}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        if is_windows():
            # os.startfile 只在 Windows 存在，Linux 的类型桩里没有这个属性。
            # 必须用**带默认值**的 getattr：写成 getattr(os, "startfile") 会被
            # ruff 的 B009 自动改回属性访问，mypy 在 Linux 上就会报 attr-defined。
            opener = getattr(os, "startfile", None)
            if opener is None:  # pragma: no cover - is_windows() 为真时一定存在
                return _failed("当前系统不支持用默认程序打开文件", "OPEN_FAILED")
            opener(str(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as exc:
        return _failed(f"无法用系统默认程序打开：{exc}", "OPEN_FAILED")

    stat = target.stat()
    lines = [
        f"已用系统默认程序打开：{target}",
        f"大小 {stat.st_size} 字节｜最后修改 "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))}",
    ]
    if target.suffix.lower() in _TEXT_SUFFIXES:
        try:
            content, truncated = read_text(str(target), _roots())
        except PathNotAllowed as exc:
            lines.append(f"回读失败：{exc.message}")
        else:
            lines.append("")
            lines.append("当前文件内容：")
            lines.append(content + ("\n…（文件过大已截断）" if truncated else ""))
            lines.append("说明：用户可能在外部程序里修改并保存，需要最新内容时重新调用 read_file。")
    else:
        lines.append(
            "该格式不是纯文本，已在系统程序中打开，暂不做正文提取；"
            "如需我理解内容，请提供可复制文本或告诉我文档大意。"
        )
    return truncate_tool_output("\n".join(lines), limit=4000)


# ---------- 写操作（需确认） ----------


class WriteFileInput(BaseModel):
    path: str = Field(description="要写入的文件路径，必须在白名单内")
    content: str = Field(description="要写入的完整文本内容")


@tool(args_schema=WriteFileInput)
async def write_file(path: str, content: str) -> str:
    """把文本写入白名单内的文件（覆盖写入）。执行前会请用户确认并展示预览。"""
    try:
        # 预览不校验白名单：还没授权时也得先让用户看清要写什么，否则是在盲批
        preview = describe_write(path, content)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        await _confirm_with_grant(
            path,
            action="write_file",
            detail=preview,
            remember_key=f"write_file:{preview['path']}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        saved = write_text(path, content, _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)
    return f"已写入 {saved}（{len(content.encode('utf-8'))} 字节）"


@tool(args_schema=PathInput)
async def create_dir(path: str) -> str:
    """在白名单内创建目录（含多级父目录）。执行前会请用户确认。"""
    try:
        target = describe_write(path, f"创建目录：{path}")
        await _confirm_with_grant(
            path, action="write_file", detail=target, remember_key="create_dir"
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        created = sandbox_create_dir(path, _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)
    return f"已创建：{created}"


@tool(args_schema=PathInput)
async def delete_path(path: str) -> str:
    """删除白名单内的文件或**空**目录。执行前会请用户确认。

    目录非空时会被拒绝，避免一条指令误删整棵树。
    """
    try:
        preview = describe_write(path, f"删除：{path}")
        await _confirm_with_grant(
            path, action="delete_path", detail=preview, remember_key="delete_path"
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        return sandbox_delete_path(path, _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)


@tool(args_schema=PathInput)
async def set_wallpaper(path: str) -> str:
    """把白名单内的图片设为桌面壁纸（仅 Windows）。执行前会请用户确认。"""
    if not is_windows():
        return "TOOL_NOT_ENABLED：set_wallpaper 仅支持 Windows，当前系统不支持"

    # 形参名必须与 args_schema 的字段名一致。这里曾经写成 image_path，
    # 而 PathInput 声明的是 path —— 模型照着 schema 传 path，
    # 进来就撞 TypeError: unexpected keyword argument 'path'。
    # 单测 test_tool_contract.py 会把这类不一致挡在提交前。
    try:
        image = describe_write(path, f"设为壁纸：{path}")
        await _confirm_with_grant(
            path, action="set_wallpaper", detail=image, remember_key="set_wallpaper"
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    # 确认之后用户可能已经改动过文件，落地前再查一次
    target = Path(image["path"])
    if not target.is_file():
        return f"TOOL_FAILED：壁纸文件不存在或不是文件：{target}"

    return await _apply_wallpaper(image["path"])


async def _apply_wallpaper(absolute_path: str) -> str:
    """通过 PowerShell 调用 SystemParametersInfo 设置壁纸。

    路径用**环境变量**传给 PowerShell，不做字符串拼接——
    否则路径里的引号等字符可能被拼进命令里，形成命令注入。
    """
    script = (
        "$code = @'\n"
        "using System.Runtime.InteropServices;\n"
        "public class Wallpaper {\n"
        '  [DllImport("user32.dll", SetLastError=true)]\n'
        "  public static extern bool SystemParametersInfo(int a, int b, string c, int d);\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $code\n"
        "$path = $env:TRAE_WALLPAPER_PATH\n"
        "if (-not (Test-Path -LiteralPath $path)) { Write-Error 'file not found'; exit 2 }\n"
        "[Wallpaper]::SystemParametersInfo(20, 0, $path, 3)\n"
    )

    env = dict(os.environ)
    env["TRAE_WALLPAPER_PATH"] = absolute_path

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )

    try:
        result = await asyncio.to_thread(_run)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"TOOL_FAILED：壁纸设置失败（{exc}）"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        return f"TOOL_FAILED：壁纸设置失败{('：' + detail) if detail else ''}"

    return f"已把 {absolute_path} 设为桌面壁纸"
