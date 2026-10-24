"""system 组工具：文件沙箱操作与桌面壁纸切换。

读操作（list_dir / read_file）不需要确认——SPEC 契约 2.4 的需确认动作清单
只有 write_file / delete_path / set_wallpaper / run_script，读不在其中，
且路径已被 ALLOWED_ROOTS 沙箱限制。

写、建目录、删、换壁纸都要先经用户确认；拿不到确认通道时一律拒绝执行。
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

from app.agent.permissions import PermissionDenied, require_permission
from app.core.config import Settings, load_settings
from app.core.context import truncate_tool_output
from app.core.security import (
    PathNotAllowed,
    allowed_roots,
    build_write_preview,
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


# ---------- 读操作（无需确认） ----------


class PathInput(BaseModel):
    path: str = Field(
        description="目标路径（绝对路径或含 ~ 的路径），必须在 ALLOWED_ROOTS 白名单内"
    )


@tool(args_schema=PathInput)
async def list_dir(path: str) -> str:
    """列出白名单目录内的文件与子目录。只读取，不修改任何内容。"""
    try:
        items = sandbox_list_dir(path, _roots())
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
        content, truncated = read_text(path, _roots())
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
        target = ensure_allowed(path, _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    if not target.is_file():
        return _failed(f"不是文件：{target}", "PATH_NOT_ALLOWED")

    try:
        await require_permission(
            "open_file",
            {
                "path": str(target),
                "preview": "将用系统默认程序打开该文件，并把可读文本内容返回给助手判断",
            },
            remember_key=f"open_file:{target}",
        )
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)

    try:
        if is_windows():
            # startfile 只在 Windows 上存在，getattr 取用可避免跨平台类型检查报错
            os.startfile(str(target))
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
        preview = build_write_preview(path, content, _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        await require_permission(
            "write_file",
            preview,
            remember_key=f"write_file:{preview['path']}",
        )
    except PermissionDenied as exc:
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
        target = build_write_preview(path, f"创建目录：{path}", _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        await require_permission("write_file", target, remember_key="create_dir")
    except PermissionDenied as exc:
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
        preview = build_write_preview(path, f"删除：{path}", _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        await require_permission("delete_path", preview, remember_key="delete_path")
    except PermissionDenied as exc:
        return _failed(exc.message, exc.code)

    try:
        return sandbox_delete_path(path, _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)


@tool(args_schema=PathInput)
async def set_wallpaper(image_path: str) -> str:
    """把白名单内的图片设为桌面壁纸（仅 Windows）。执行前会请用户确认。"""
    if not is_windows():
        return "TOOL_NOT_ENABLED：set_wallpaper 仅支持 Windows，当前系统不支持"

    try:
        image = build_write_preview(image_path, f"设为壁纸：{image_path}", _roots())
    except PathNotAllowed as exc:
        return _failed(exc.message, exc.code)

    try:
        await require_permission("set_wallpaper", image, remember_key="set_wallpaper")
    except PermissionDenied as exc:
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
