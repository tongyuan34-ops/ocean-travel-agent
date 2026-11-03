"""路径沙箱：所有文件操作必须落在白名单内。

三条铁律：
1. **先 resolve 再校验**：解析软链接与 `..`，否则 `白名单/../../etc` 能绕过去
2. **按路径层级比较，不用字符串前缀**：否则 `/data-backup` 会被 `/data` 放行
3. **白名单为空时一律拒绝**：宁可工具不可用，也不能默认放开

**白名单从哪来**

配置里的 ``ALLOWED_ROOTS``（用户手写）+ ``grants.json``（用户在使用过程中
按需授权的目录，见 ``app/core/grants.py``）。两处相加，不互相覆盖——
环境变量优先级最高，如果授权也写进 ``ALLOWED_ROOTS``，.env 里有一份就
把授权盖掉了。
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from app.core import grants
from app.core.config import PROJECT_ROOT, Settings

# 单次读取上限，避免把大文件塞爆上下文
MAX_READ_BYTES = 200_000
# 预览内容上限，用于写文件前的确认展示
PREVIEW_LIMIT = 1200


class PathNotAllowed(Exception):
    """路径越界。错误码与 SPEC 契约一致。"""

    code = "PATH_NOT_ALLOWED"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GrantNeeded(PathNotAllowed):
    """越界了，但**可以**请用户授权——不是硬拒绝。

    和父类的区别只有一个：``suggested_root`` 有值时，工具层可以弹一次
    「允许访问 X 目录吗」；为 None 时（系统目录、盘根）连问都不该问。
    """

    def __init__(self, target: Path, suggested: Path | None) -> None:
        if suggested is None:
            super().__init__(f"这个位置不允许访问：{target}")
        else:
            super().__init__(f"需要先授权访问 {suggested} 才能操作 {target}")
        self.target = target
        self.suggested_root = suggested


def is_windows() -> bool:
    return platform.system() == "Windows"


def allowed_roots(settings: Settings) -> list[Path]:
    """配置里的白名单 + 用户在使用中授权过的目录。

    授权是追加的，不是替换：用户手写的 ``ALLOWED_ROOTS`` 永远有效，
    按需授权只是在它后面加一条。
    """
    roots: list[Path] = []
    for raw in settings.allowed_roots:
        try:
            resolved = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if resolved not in roots:
            roots.append(resolved)

    for granted in grants.load():
        if granted not in roots:
            roots.append(granted)
    return roots


def _volume_roots() -> list[Path]:
    """盘符根。只能「整块」拒绝，不能「在里面就拒绝」——否则 C:\\ 会把 C:\\Users 也否掉。"""
    if is_windows():
        return [Path(f"{os.environ.get('SYSTEMDRIVE', 'C:')}\\")]
    return [Path("/")]


def _protected_trees() -> list[Path]:
    """这些目录连问都不问：用户点「允许」也不该打开系统目录。"""
    if is_windows():
        drive = Path(f"{os.environ.get('SYSTEMDRIVE', 'C:')}\\")
        return [
            drive / "Windows",
            drive / "Program Files",
            drive / "Program Files (x86)",
            drive / "ProgramData",
            drive / "Recovery",
        ]
    return [Path(p) for p in ("/etc", "/usr", "/bin", "/sbin", "/System", "/Library", "/private")]


def _is_protected(path: Path) -> bool:
    if any(path == root for root in _volume_roots()):
        return True
    return any(path == root or _is_within(path, root) for root in _protected_trees())


def _user_dirs() -> list[Path]:
    """用户最可能想开放的那几个目录。存在才算——路径被改到别的盘时不硬猜。"""
    home = Path.home()
    names = ("Desktop", "Documents", "Downloads", "Pictures")
    found = [home / name for name in names if (home / name).is_dir()]
    return [*found, home]


def suggest_grant_root(target: Path) -> Path | None:
    """越界路径该请用户授权哪个目录。``None`` 表示这个位置不该开放。

    优先给「桌面 / 文档 / 下载 / 图片」这类用户认得的一级目录，而不是文件的
    父目录——授权一次能覆盖同目录下后续的所有操作，用户也不用判断
    「授权这一层够不够」。
    """
    try:
        resolved = target.expanduser().resolve()
    except (OSError, RuntimeError):
        return None

    if _is_protected(resolved):
        return None

    for known in _user_dirs():
        if _is_within(resolved, known):
            return known

    parent = resolved.parent
    if parent == resolved or _is_protected(parent):
        return None
    return parent


def ensure_allowed(target: str, roots: list[Path]) -> Path:
    """校验目标路径在白名单内，返回 resolve 后的路径。

    越界时抛 ``GrantNeeded``（``PathNotAllowed`` 的子类）：工具层可以据此
    请用户授权一次。真正不该开放的位置 ``suggested_root`` 是 None。
    """
    try:
        resolved = Path(target).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise PathNotAllowed(f"路径无法解析：{exc}") from exc

    for root in roots:
        if _is_within(resolved, root):
            return resolved

    raise GrantNeeded(resolved, suggest_grant_root(resolved))


def _is_within(path: Path, root: Path) -> bool:
    """按路径层级判断包含关系。"""
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        # 不同盘符（Windows）会抛 ValueError
        return False


def list_dir(target: str, roots: list[Path]) -> list[dict[str, str]]:
    directory = ensure_allowed(target, roots)
    if not directory.is_dir():
        raise PathNotAllowed(f"不是目录：{directory}")

    items: list[dict[str, str]] = []
    for entry in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name)):
        kind = "dir" if entry.is_dir() else "file"
        size = ""
        if entry.is_file():
            try:
                size = f"{entry.stat().st_size} B"
            except OSError:
                size = "未知"
        items.append({"name": entry.name, "kind": kind, "size": size})
    return items


def read_text(target: str, roots: list[Path]) -> tuple[str, bool]:
    """读取文本文件，返回 (内容, 是否被截断)。"""
    file_path = ensure_allowed(target, roots)
    if not file_path.is_file():
        raise PathNotAllowed(f"不是文件：{file_path}")

    try:
        size = file_path.stat().st_size
        raw = file_path.read_bytes()
    except OSError as exc:
        raise PathNotAllowed(f"读取失败：{exc}") from exc

    truncated = size > MAX_READ_BYTES
    raw = raw[:MAX_READ_BYTES]
    return raw.decode("utf-8", errors="replace"), truncated


def describe_write(target: str, content: str) -> dict[str, str]:
    """描述一次写入，**不校验白名单**。

    单独拆出来是为了「授权 + 写入」能合并成一次确认：还没授权时也得先给用户
    看清楚要写什么，否则用户是在盲批。真正落盘仍然走 ``write_text`` 的校验。
    """
    try:
        file_path = Path(target).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise PathNotAllowed(f"路径无法解析：{exc}") from exc

    preview = content[:PREVIEW_LIMIT]
    if len(content) > PREVIEW_LIMIT:
        preview += f"\n…（预览截断，全文共 {len(content)} 字）"
    return {
        "path": str(file_path),
        "exists": "true" if file_path.exists() else "false",
        "bytes": str(len(content.encode("utf-8"))),
        "preview": preview,
    }


def build_write_preview(target: str, content: str, roots: list[Path]) -> dict[str, str]:
    """写文件前的预览，供前端展示给用户确认。"""
    file_path = ensure_allowed(target, roots)
    return describe_write(str(file_path), content)


def write_text(target: str, content: str, roots: list[Path]) -> str:
    file_path = ensure_allowed(target, roots)
    if file_path.exists() and file_path.is_dir():
        raise PathNotAllowed(f"目标是一个目录，不能写入文件：{file_path}")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise PathNotAllowed(f"写入失败：{exc}") from exc
    return str(file_path)


def create_dir(target: str, roots: list[Path]) -> str:
    directory = ensure_allowed(target, roots)
    if directory.exists():
        return f"{directory}（已存在）"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathNotAllowed(f"创建目录失败：{exc}") from exc
    return str(directory)


def delete_path(target: str, roots: list[Path]) -> str:
    """删除。目录只允许删空的，避免一条指令误删整棵树。"""
    resolved = ensure_allowed(target, roots)

    # 不允许删白名单根目录本身
    if resolved in roots:
        raise PathNotAllowed(f"不允许删除白名单根目录：{resolved}")

    if resolved.is_dir():
        try:
            remaining = list(resolved.iterdir())
        except OSError as exc:
            raise PathNotAllowed(f"读取目录失败：{exc}") from exc
        if remaining:
            raise PathNotAllowed(
                f"目录非空（{len(remaining)} 项），已拒绝删除：{resolved}。"
                "请先清空目录，或逐个删除文件。"
            )
        try:
            resolved.rmdir()
        except OSError as exc:
            raise PathNotAllowed(f"删除目录失败：{exc}") from exc
        return f"已删除空目录 {resolved}"

    if not resolved.exists():
        raise PathNotAllowed(f"目标不存在：{resolved}")

    try:
        resolved.unlink()
    except OSError as exc:
        raise PathNotAllowed(f"删除文件失败：{exc}") from exc
    return f"已删除文件 {resolved}"


# ---------- 凭据：登录态 Cookie（plugin 组 B） ----------


def cookie_dir(settings: Settings) -> Path:
    """Cookie 存放目录。相对路径按项目根解析，避免受进程 CWD 影响。"""
    raw = Path(settings.cookie_dir).expanduser()
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def cookie_path(settings: Settings, platform: str) -> Path:
    """某平台 Cookie 的文件路径：`<COOKIE_DIR>/<平台名>.txt`。"""
    return cookie_dir(settings) / f"{platform}.txt"


def read_cookie(settings: Settings, platform: str) -> str:
    """读取某平台已保存的 Cookie 串；不存在或读失败返回空串。

    存储约定与 scripts/apply_cookies.py 一致：文件内容就是完整 Cookie 串。
    读取方（采集适配器）不关心它长什么样，只判断"有没有"。
    """
    path = cookie_path(settings, platform)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
