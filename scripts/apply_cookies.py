"""登录态 Cookie 管理（plugin 组 B，见 SPEC 4.7）。

三种用法：

    python scripts/apply_cookies.py                          # 交互式：选平台 → 粘贴 Cookie → 落盘
    python scripts/apply_cookies.py --from-file <文件>        # 从文件导入（支持 Netscape cookies.txt）
    python scripts/apply_cookies.py --status                 # 查看各平台就绪状态与环境开关

存储约定：**每个平台一个纯文本文件**，路径为 `COOKIE_DIR/<平台名>.txt`，
文件内容就是该平台的完整 Cookie 串（单行 `k=v; k=v`），不带注释头——
这样任何下游（MediaCrawler 的 --cookies、Playwright 的 Cookie 头）都能原样读走。

安全提醒：
- Cookie 等同于账号登录态，**本脚本不回显 Cookie 内容**，只报告长度与关键字段命中情况
- 文件是明文，未加密；请靠 .gitignore 与文件权限保护，切勿提交到仓库或粘贴到对话里
- 抓取平台内容违反其服务条款且有封号风险，本功能默认关闭、个人自用、不随核心开源发布
"""

from __future__ import annotations

import argparse
import getpass
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, load_settings
from app.core.security import cookie_dir, cookie_path


class CookieError(Exception):
    """Cookie 内容或参数不合法。"""


@dataclass(frozen=True)
class ParsedFile:
    """导入文件的解析结果。

    skipped 是**格式不符被跳过的行数**——必须显式报出来，
    否则用户以为导入了 10 条、实际只有 3 条进了 Cookie 串，还查不出原因。
    """

    cookie: str
    platform: str
    entries: int
    skipped: int


@dataclass(frozen=True)
class Platform:
    """平台元信息。

    crawler_code 为 MediaCrawler 的 --platform 取值；空表示该平台不走 MediaCrawler。
    required 为登录态必须包含的关键字段——缺了它就是未登录状态，
    采集会静默返回空结果，因此默认直接拦下（可用 --force 跳过）。
    """

    name: str
    label: str
    crawler_code: str
    required: tuple[str, ...]


PLATFORMS: tuple[Platform, ...] = (
    Platform("douyin", "抖音", "dy", ("sessionid",)),
    Platform("xiaohongshu", "小红书", "xhs", ("web_session",)),
    Platform("bilibili", "B站", "bili", ("SESSDATA",)),
    Platform("weibo", "微博", "wb", ("SUB",)),
    Platform("zhihu", "知乎", "zhihu", ("z_c0",)),
    Platform("tieba", "贴吧", "tieba", ("BDUSS",)),
    Platform("xiaoheihe", "小黑盒", "", ()),  # Playwright 监听页面原生请求，无固定字段
    Platform("boss_zhipin", "BOSS直聘", "", ("__zp_stoken__",)),
)

_BY_NAME = {item.name: item for item in PLATFORMS}

# Netscape cookies.txt 的域名 → 平台名，用于 --from-file 自动识别平台
_DOMAIN_HINTS: tuple[tuple[str, str], ...] = (
    ("douyin.com", "douyin"),
    ("xiaohongshu.com", "xiaohongshu"),
    ("xhslink.com", "xiaohongshu"),
    ("bilibili.com", "bilibili"),
    ("weibo.com", "weibo"),
    ("weibo.cn", "weibo"),
    ("zhihu.com", "zhihu"),
    ("tieba.baidu.com", "tieba"),
    ("xiaoheihe", "xiaoheihe"),
    ("heybox", "xiaoheihe"),
    ("zhipin.com", "boss_zhipin"),
)


# ---------- 校验与解析 ----------


def parse_pairs(cookie: str) -> dict[str, str]:
    """把 Cookie 串解析成 name → value（只用于校验，不外传）。"""
    pairs: dict[str, str] = {}
    for chunk in cookie.split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name = name.strip()
        if name:
            pairs[name] = value.strip()
    return pairs


def normalize_cookie(raw: str) -> str:
    """清掉粘贴时常见的外层引号、换行与多余分号，压缩成单行 Cookie 串。"""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1]
    chunks = [chunk.strip() for chunk in text.replace("\n", ";").split(";")]
    return "; ".join(chunk for chunk in chunks if chunk)


def validate(platform: Platform, cookie: str, *, force: bool) -> None:
    """校验 Cookie 形态与关键字段。挂在关键字段上是刻意的：
    缺 sessionid 的"抖音 Cookie"其实是未登录态，采集只会返回空，
    与其事后排查不如在这里就报错。"""
    if "=" not in cookie:
        raise CookieError("这不像是 Cookie 串：至少应包含 name=value 形式的内容")

    if not platform.required:
        return

    pairs = parse_pairs(cookie)
    missing = [key for key in platform.required if key not in pairs]
    if missing and not force:
        raise CookieError(
            f"{platform.label} 的 Cookie 缺少关键字段：{'、'.join(missing)}。"
            "缺了它采集会以未登录状态运行、且只会返回空结果。"
            "确认无误可加 --force 跳过校验"
        )


def save(settings: Settings, platform: Platform, cookie: str) -> Path:
    target = cookie_path(settings, platform.name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(cookie + "\n", encoding="utf-8")
    return target


# ---------- 三种用法 ----------


def _parse_from_file(path: Path) -> ParsedFile:
    """解析导入文件。

    支持两种格式：
    1. Netscape cookies.txt（浏览器扩展导出）——制表符分隔，取第 6、7 列的 name/value
    2. 纯文本 Cookie 串——一行或按行折行的 `k=v`
    """
    if not path.is_file():
        raise CookieError(f"文件不存在：{path}")

    lines = [
        line
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise CookieError(f"文件里没有有效内容：{path}")

    # 用「是否存在 ≥7 列的制表符行」作为 Netscape 格式的判据，
    # 而不是「所有行都含制表符」——否则文件里混进一行坏行，
    # 整个文件会被误判成纯文本 Cookie 串，解析结果全错。
    if any(len(line.split("\t")) >= 7 for line in lines):
        pairs: list[str] = []
        domains: list[str] = []
        skipped = 0
        for line in lines:
            fields = line.split("\t")
            if len(fields) < 7 or not fields[5].strip():
                skipped += 1
                continue
            domains.append(fields[0].strip())
            pairs.append(f"{fields[5].strip()}={fields[6].strip()}")
        if not pairs:
            raise CookieError(
                "解析不出任何 Cookie 条目——请确认是 Netscape cookies.txt 格式"
                "（每行 7 列、制表符分隔）"
            )
        return ParsedFile(
            cookie="; ".join(pairs),
            platform=_infer_platform(domains),
            entries=len(pairs),
            skipped=skipped,
        )

    return ParsedFile(
        cookie=normalize_cookie("; ".join(lines)),
        platform="",
        entries=len(lines),
        skipped=0,
    )


def _infer_platform(domains: list[str]) -> str:
    joined = " ".join(domains).lower()
    for hint, name in _DOMAIN_HINTS:
        if hint in joined:
            return name
    return ""


def _resolve_platform(raw: str) -> Platform:
    key = raw.strip().lower()
    if key in _BY_NAME:
        return _BY_NAME[key]
    if key.isdigit() and 1 <= int(key) <= len(PLATFORMS):
        return PLATFORMS[int(key) - 1]
    raise CookieError(f"未知平台：{raw}。可用：{'、'.join(_BY_NAME)}")


def _describe(platform: Platform, cookie: str) -> str:
    """只报告形态与关键字段命中情况，绝不回显 Cookie 内容。"""
    pairs = parse_pairs(cookie)
    detail = f"{len(cookie)} 字符、{len(pairs)} 个字段"
    if platform.required:
        hit = "、".join(f"{key}{'✓' if key in pairs else '✗'}" for key in platform.required)
        detail += f"；关键字段 {hit}"
    return detail


def run_interactive(settings: Settings, platform: Platform | None, force: bool) -> int:
    if platform is None:
        print("可用平台：")
        for index, item in enumerate(PLATFORMS, start=1):
            code = f"MediaCrawler 代码 {item.crawler_code}" if item.crawler_code else "独立方案"
            print(f"  {index}. {item.name:<12} {item.label:<10} {code}")
        choice = input("\n请选择平台（序号或名称）：").strip()
        platform = _resolve_platform(choice)

    print(f"\n请粘贴 {platform.label} 的 Cookie。")
    print("获取方式：浏览器登录目标平台网页版 → F12 → 网络 → 刷新 → 点任一请求")
    print("          → 请求标头 → 复制 Cookie: 冒号后面的完整内容")
    print("（粘贴时不会回显，粘贴后直接回车）\n")

    raw = getpass.getpass(f"{platform.label} Cookie> ")
    cookie = normalize_cookie(raw)
    if not cookie:
        raise CookieError("没有读取到内容，已取消")

    validate(platform, cookie, force=force)
    target = save(settings, platform, cookie)
    print(f"\n已保存 {platform.label} 的 Cookie：{target}")
    print(f"  {_describe(platform, cookie)}")
    print("  内容未回显，也未被记录到任何日志。")
    return 0


def run_from_file(settings: Settings, path: Path, platform_name: str, force: bool) -> int:
    parsed = _parse_from_file(path)
    cookie = normalize_cookie(parsed.cookie)

    raw_name = platform_name.strip() or parsed.platform
    if not raw_name:
        raise CookieError("无法从文件中推断平台，请用 --platform 指定（如 --platform douyin）")
    platform = _resolve_platform(raw_name)
    if parsed.platform and platform_name.strip() and parsed.platform != platform.name:
        print(f"注意：文件域名指向 {parsed.platform}，但你指定的是 {platform.name}，按你指定的处理")
    if parsed.skipped:
        print(f"注意：有 {parsed.skipped} 行格式不符已被跳过（应为 7 列制表符分隔）")

    validate(platform, cookie, force=force)
    target = save(settings, platform, cookie)
    print(f"已从 {path} 导入 {platform.label} 的 Cookie：{target}")
    print(f"  共 {parsed.entries} 个条目；{_describe(platform, cookie)}")
    return 0


def _is_wide(ch: str) -> bool:
    """CJK 字符在终端里占两列。"""
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    )


def _pad(text: str, width: int) -> str:
    """按显示宽度左侧对齐补齐，避免中英混排时表格错位。"""
    gap = width - sum(2 if _is_wide(ch) else 1 for ch in text)
    return text + " " * gap if gap > 0 else text


def run_status(settings: Settings) -> int:
    directory = cookie_dir(settings)
    print("=== 登录态采集环境 ===")
    print(
        f"ENABLE_SESSION_FETCH = {str(settings.enable_session_fetch).lower()}"
        "   （默认关闭；置 true 后前端才会展示社媒入口）"
    )
    print(f"COOKIE_DIR           = {directory}{'' if directory.exists() else '（不存在）'}")
    print(
        f"MEDIACRAWLER_PATH    = {settings.mediacrawler_path}"
        f"{'' if Path(settings.mediacrawler_path).exists() else '（未部署）'}"
    )
    print(f"MEDIACRAWLER_TIMEOUT = {settings.mediacrawler_timeout} 秒")
    print(f"CRAWLER_PLATFORMS    = {','.join(settings.crawler_platforms)}")

    print("\n=== 各平台 Cookie 状态 ===")
    print(_pad("平台", 14) + _pad("名称", 10) + _pad("代码", 8) + _pad("状态", 26) + "更新时间")
    ready = 0
    for platform in PLATFORMS:
        path = cookie_path(settings, platform.name)
        code = platform.crawler_code or "-"
        if path.is_file():
            cookie = path.read_text(encoding="utf-8").strip()
            pairs = parse_pairs(cookie)
            missing = [key for key in platform.required if key not in pairs]
            if missing:
                state = f"已保存，缺 {'、'.join(missing)}"
            else:
                state = f"已保存（{len(cookie)} 字符）"
                ready += 1
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        else:
            state = "未保存"
            stamp = "-"
        print(
            _pad(platform.name, 14)
            + _pad(platform.label, 10)
            + _pad(code, 8)
            + _pad(state, 26)
            + stamp
        )

    print(f"\n可用平台：{ready}/{len(PLATFORMS)}")
    print("提醒：Cookie 为明文且会失效（通常几天到几周），失效后需重新导出；")
    print("      请确认 .gitignore 已屏蔽 .env 与 cookies/，切勿提交或粘贴到对话里。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="登录态 Cookie 管理（默认关闭的社媒采集插件，个人自用）"
    )
    parser.add_argument("--platform", default="", help="平台名或序号，跳过交互式选择")
    parser.add_argument("--from-file", dest="from_file", default="", help="从文件导入 Cookie")
    parser.add_argument("--status", action="store_true", help="查看各平台就绪状态与开关")
    parser.add_argument(
        "--force", action="store_true", help="跳过关键字段校验（确认自己清楚风险时使用）"
    )
    args = parser.parse_args()

    settings = load_settings()

    try:
        if args.status:
            return run_status(settings)

        if args.from_file:
            platform_name = args.platform
            return run_from_file(
                settings, Path(args.from_file).expanduser(), platform_name, args.force
            )

        platform = _resolve_platform(args.platform) if args.platform else None
        return run_interactive(settings, platform, args.force)
    except CookieError as exc:
        print(f"\n失败：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
