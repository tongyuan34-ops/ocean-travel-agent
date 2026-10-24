"""MediaCrawler 社媒适配器（plugin 组 B：默认关闭、个人自用、不随核心开源发布）。

桥接方式（SPEC 4.7）：**子进程调用** MediaCrawler 的 main.py，读它落盘的 JSON 结果。
不 import 它的包——两套依赖（本项目 3.13 / MediaCrawler 3.11）必须完全隔离，
否则迟早互相污染。

三条硬性约束：

1. **默认关闭**：`ENABLE_SESSION_FETCH=false` 时 available() 直接 False，
   绝不"顺手"跑一次；未实现的功能也不得谎报为可用。
2. **没 Cookie 不跑**：缺对应平台 Cookie 时 available()=False，让调度层跳过。
   带着未登录态硬跑只会返回空结果，白等几分钟。
3. **子进程必须能被可靠掐断**：外层超时严格大于内层超时；超时后用
   `taskkill /F /T` 清理整棵进程树，否则会留下孤儿 Chromium 进程堆在后台。

Cookie **绝不写入日志、绝不出现在返回文本里**——它等同于账号登录态。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.base import SourceAdapter
from app.core.config import PROJECT_ROOT, Settings
from app.core.models import Doc, SourceKind, source_kind_value
from app.core.security import read_cookie

logger = logging.getLogger(__name__)

# 源外层超时比内层（MEDIACRAWLER_TIMEOUT）多出的余量。
# SPEC 4.8 铁律：外层超时必须 ≥ 内层超时，否则子进程还没跑完就被提前杀掉。
MEDIACRAWLER_OVERHEAD_SEC = 20

# 平台注册表：(canonical 名, MediaCrawler 代码, 前端展示名)
PLATFORM_SPECS: tuple[tuple[str, str, str], ...] = (
    ("douyin", "dy", "抖音"),
    ("xiaohongshu", "xhs", "小红书"),
    ("bilibili", "bili", "B站"),
    ("weibo", "wb", "微博"),
    ("zhihu", "zhihu", "知乎"),
    ("tieba", "tieba", "贴吧"),
)

PLATFORM_LABELS = {key: label for key, _code, label in PLATFORM_SPECS}
PLATFORM_CODES = {key: code for key, code, _label in PLATFORM_SPECS}
SOCIAL_PLATFORM_KEYS = tuple(PLATFORM_LABELS)

# 各平台内容字段名差异很大，用「候选键逐个探测」而不是按平台写分支——
# SPEC 要求所有源统一归一化为 Doc，写了特判就再也统一不起来了。
_ID_KEYS = ("note_id", "aweme_id", "video_id", "content_id", "mblog_id", "id")
_URL_KEYS = ("note_url", "video_url", "content_url", "mblog_url", "share_url", "url")
_TITLE_KEYS = ("title", "display_title", "note_title", "desc", "content")
_DESC_KEYS = ("desc", "content", "text", "content_text")
_AUTHOR_KEYS = ("nickname", "user_nickname", "author", "user_name", "name")
# SPEC 4.7 的平台点赞字段 fallback：bili=like_count、wb=comment_like_count、
# zhihu=like_count、tieba 无点赞（缺了就为 0）
_LIKE_KEYS = (
    "liked_count",
    "like_count",
    "digg_count",
    "comment_like_count",
    "link_award_num",
)
_TIME_KEYS = ("create_time", "publish_time", "create_date_time", "created_at", "time")
_COMMENT_TEXT_KEYS = ("content", "comment_text", "text")

# 只对「URL 字段缺失」的平台做模板兜底
_URL_TEMPLATES = {
    "douyin": "https://www.douyin.com/video/{0}",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/{0}",
}

_MAX_HOT_COMMENTS = 3


@dataclass(slots=True)
class CrawlerRun:
    """一次子进程执行的结果。"""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def tail(self, limit: int = 400) -> str:
        """取输出末尾用于报错。不包含命令行（命令行里有 Cookie）。"""
        text = (self.stderr or self.stdout or "").strip()
        text = "\n".join(line for line in text.splitlines() if line.strip())
        return text[-limit:] if len(text) > limit else text


async def run_crawler_subprocess(
    cmd: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
) -> CrawlerRun:
    """执行采集子进程，超时则连带进程树清理。

    单独抽出来是为了能被独立验证——真正跑采集需要真实 Cookie 与真实平台，
    而超时/清理这条路径必须能确定性地测到。
    """
    # POSIX 上必须让子进程成为独立会话的首进程。
    # 否则它继承本进程的进程组，_kill_process_tree 里的 os.getpgid(pid)
    # 拿到的就是**我们自己**的组号，killpg 会把 API 服务/测试进程一起 SIGKILL 掉。
    # Windows 走 taskkill /T，不需要也无法用这个参数。
    spawn_kwargs: dict[str, Any] = {}
    if sys.platform != "win32":
        spawn_kwargs["start_new_session"] = True

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **spawn_kwargs,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        await _kill_process_tree(process.pid)
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except TimeoutError:  # pragma: no cover - 极端情况
            logger.error("子进程 %s 清理后仍未退出", process.pid)
        return CrawlerRun(
            returncode=-1,
            stdout="",
            stderr=f"超时（>{timeout:.0f}s）",
            timed_out=True,
        )

    return CrawlerRun(
        returncode=process.returncode or 0,
        stdout=_decode(stdout),
        stderr=_decode(stderr),
    )


async def _kill_process_tree(pid: int) -> None:
    """Windows 用 taskkill /F /T 连带子树；其他平台杀进程组。

    MediaCrawler 会拉起 Chromium，只 kill 父进程会留下孤儿浏览器，
    因此必须按树清理（SPEC 4.8）。

    **绝不能误杀自己**：子进程由 run_crawler_subprocess 以 start_new_session
    启动，正常情况组号等于它自己的 pid。这里再做一次比对，
    万一将来有人改了启动参数、子进程又落回本进程组，宁可只杀单个进程，
    也不能把整个服务端进程组带走。
    """
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
        return

    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return

    if pgid == os.getpgid(0):
        logger.error(
            "拒绝清理进程组 %s：与当前进程同组，退化为只杀该进程。"
            "请检查子进程是否漏了 start_new_session",
            pgid,
        )
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        return

    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


class MediaCrawlerAdapter(SourceAdapter):
    """一个平台一个适配器实例，name 直接是平台名（douyin / xiaohongshu / ...）。

    这样 capabilities.platforms 里的取值就是平台名，前端可直接用于来源标签。
    """

    # 社媒采集耗时长、且带风控与封号风险，**只应由用户请求触发的工具显式调度**，
    # 不参与"通用检索"的源自选，避免被模型当成搜索引擎随手调用。
    selectable_by_llm = False

    def __init__(self, settings: Settings, key: str) -> None:
        super().__init__(settings)
        self.name = key
        self.platform = PLATFORM_LABELS[key]
        self.kind = SourceKind.SOCIAL_CN
        self.crawler_code = PLATFORM_CODES[key]
        self.search_timeout = float(settings.mediacrawler_timeout + MEDIACRAWLER_OVERHEAD_SEC)

    # ---------- 可用性 ----------

    def available(self) -> bool:
        return not self._blockers()

    def unavailable_reason(self) -> str:
        return "；".join(self._blockers())

    def _blockers(self) -> list[str]:
        """按顺序列出"为什么现在不能跑"。逐条给出，便于用户一步到位修好。"""
        reasons: list[str] = []
        if not self._settings.enable_session_fetch:
            reasons.append("ENABLE_SESSION_FETCH 未开启（该插件默认关闭）")
        if not self._python_path().is_file():
            reasons.append(f"未找到 MediaCrawler 虚拟环境：{self._python_path()}")
        if not self._entry().is_file():
            reasons.append(f"未找到 MediaCrawler 入口：{self._entry()}")
        if not self._cookie():
            reasons.append("未配置该平台 Cookie（用 scripts/apply_cookies.py 写入）")
        return reasons

    # ---------- 路径与凭据 ----------

    def _root(self) -> Path:
        return Path(self._settings.mediacrawler_path).expanduser()

    def _python_path(self) -> Path:
        return self._root() / ".venv" / "Scripts" / "python.exe"

    def _entry(self) -> Path:
        return self._root() / "main.py"

    def _cookie(self) -> str:
        return read_cookie(self._settings, self.name)

    def _node_dir(self) -> Path:
        """Playwright 自带的 node 所在目录。"""
        return self._root() / ".venv" / "Lib" / "site-packages" / "playwright" / "driver"

    def _subprocess_env(self) -> dict[str, str] | None:
        """给采集子进程准备环境变量；无需改动时返回 None（继承父进程）。

        抖音与知乎的请求签名要跑 JS（PyExecJS）。本机若没有 Node，
        PyExecJS 会退回 Windows 自带的 JScript——那是个 ES3 引擎，
        一遇到现代语法就 `SyntaxError: 缺少 ')'`（实测抖音就是这么挂的），
        整个采集静默失败。Playwright 自己就带了一个 node，
        挂到 PATH 并指定 EXECJS_RUNTIME=Node，就不必再要求用户装 Node.js。

        只在系统确实没有 node 时才接管，避免覆盖用户自己的 Node 环境。
        """
        if shutil.which("node"):
            return None
        node_exe = self._node_dir() / "node.exe"
        if not node_exe.is_file():
            return None

        env = dict(os.environ)
        env["PATH"] = f"{self._node_dir()}{os.pathsep}{env.get('PATH', '')}"
        env.setdefault("EXECJS_RUNTIME", "Node")
        logger.info("采集子进程已挂载 Playwright 自带 node：%s", node_exe)
        return env

    def _data_dir(self) -> Path:
        """结果落盘目录。

        用 --save_data_path 显式指定成项目根的 data/，
        而不是让 MediaCrawler 按自己的 CWD 相对落盘——否则结果位置随
        调用方 CWD 漂移，读取端根本找不到。
        """
        return PROJECT_ROOT / "data"

    # ---------- 检索 ----------

    async def _search(self, query: str, limit: int) -> list[Doc]:
        if not self._cookie():
            # 双保险：available() 已经拦过一次，这里再拦一次，
            # 防止将来有人绕过调度层直接调 _search 而带着未登录态硬跑
            raise RuntimeError("未配置该平台 Cookie，拒绝以未登录态运行")

        started = time.time()
        cmd = self._build_command(query, limit)
        run = await run_crawler_subprocess(
            cmd,
            cwd=str(self._root()),
            timeout=float(self._settings.mediacrawler_timeout),
            env=self._subprocess_env(),
        )

        if run.timed_out:
            raise TimeoutError(
                f"MediaCrawler 超时（>{self._settings.mediacrawler_timeout}s），已清理进程树"
            )

        docs = collect_docs(
            platform_key=self.name,
            label=self.platform,
            code=self.crawler_code,
            data_dir=self._data_dir(),
            since=started,
            limit=max(1, limit),
            query=query,
        )
        if not docs and run.returncode != 0:
            raise RuntimeError(f"MediaCrawler 退出码 {run.returncode}：{run.tail() or '无输出'}")
        if not docs:
            logger.info(
                "MediaCrawler %s 本次无新结果（关键词=%s，退出码=%s）",
                self.name,
                query,
                run.returncode,
            )
        return docs

    def _build_command(self, query: str, limit: int) -> list[str]:
        """构造 MediaCrawler 命令行（与 SPEC 4.7 一致）。

        参数以列表形式传给 create_subprocess_exec，**不经过 shell**，
        因此 Cookie 里的特殊字符不会被解释成命令，注入无从谈起。
        """
        return [
            str(self._python_path()),
            str(self._entry()),
            "--platform",
            self.crawler_code,
            "--lt",
            "cookie",
            "--type",
            "search",
            "--keywords",
            query,
            "--cookies",
            self._cookie(),
            "--save_data_option",
            "json",
            "--save_data_path",
            str(self._data_dir()),
            "--crawler_max_notes_count",
            str(max(1, min(limit, 20))),
            "--max_comments_count_singlenotes",
            str(_MAX_HOT_COMMENTS),
            "--get_comment",
            "yes",
            "--get_sub_comment",
            "no",
            "--get_media",
            "no",
            "--headless",
            "yes",
        ]


def build_social_adapters(settings: Settings) -> list[MediaCrawlerAdapter]:
    """**始终构造**全部平台适配器：可用性由 available() 实时判断，
    这样用户中途配置好 Cookie 或打开开关后立刻生效，不用重启重建注册表。
    """
    return [MediaCrawlerAdapter(settings, key) for key in SOCIAL_PLATFORM_KEYS]


# ---------- 结果解析 ----------


def collect_docs(
    *,
    platform_key: str,
    label: str,
    code: str,
    data_dir: Path,
    since: float,
    limit: int,
    query: str = "",
) -> list[Doc]:
    """读取本次新落盘的结果文件，归一化为 Doc。

    只取 mtime 不早于本次启动的文件——MediaCrawler 是**追加/重写**同一个
    日期文件，不按 mtime 过滤就会把昨天、甚至上次未登录时的空结果混进来。

    同一天跑多个关键词时，MediaCrawler 会把结果**追加**进同一个日期文件，
    按顺序取前 limit 条会读到上一次关键词的旧内容（实测搜「稻城」却返回「大理」）。
    因此传入 query 时优先只保留 source_keyword 匹配的条目。
    """
    contents: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    for base in _candidate_result_dirs(data_dir, code, platform_key):
        contents.extend(_read_recent(base, "search_contents", since))
        comments.extend(_read_recent(base, "search_comments", since))

    if query:
        matched = [
            item
            for item in contents
            if isinstance(item, dict)
            and str(_pick(item, ("source_keyword",)) or "").strip().lower() == query.strip().lower()
        ]
        if matched:
            contents = matched

    ids = {str(_pick(item, _ID_KEYS) or "") for item in contents if isinstance(item, dict)}
    ids.discard("")
    comment_index = _index_comments(comments, ids)

    docs: list[Doc] = []
    seen: set[str] = set()
    for item in contents:
        if not isinstance(item, dict):
            continue
        content_id = str(_pick(item, _ID_KEYS) or "").strip()
        # 候选目录可能都存在，同一条内容会被读到两次，按内容 id 去重（无 id 时退回链接/标题）
        dedupe_key = content_id or str(_pick(item, _URL_KEYS) or _pick(item, _TITLE_KEYS) or "")
        if dedupe_key and dedupe_key in seen:
            continue
        if dedupe_key:
            seen.add(dedupe_key)
        docs.append(
            _to_doc(
                platform_key=platform_key,
                label=label,
                item=item,
                comments=comment_index.get(content_id, []),
            )
        )
        if len(docs) >= limit:
            break
    return docs


def _candidate_result_dirs(data_dir: Path, code: str, platform_key: str) -> list[Path]:
    """采集结果的候选目录。

    MediaCrawler 各平台落盘的目录名**并不统一**：bilibili 写 `data/bili`，
    douyin 写 `data/douyin`，weibo 写 `data/weibo`（而不是 CLI 代码 `wb`）。
    所以两个名字都试一遍，而不是去猜每个平台用哪个——
    猜错的表现是"采集其实成功了，但读端找不到文件、报 0 条"，最难排查。
    """
    ordered = [Path(data_dir) / code / "json", Path(data_dir) / platform_key / "json"]
    unique: list[Path] = []
    for path in ordered:
        if path not in unique:
            unique.append(path)
    return unique


def _read_recent(base: Path, prefix: str, since: float) -> list[dict[str, Any]]:
    if not base.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"{prefix}_*")):
        if path.suffix not in {".json", ".jsonl"}:
            continue
        try:
            if path.stat().st_mtime < since - 1.0:
                continue
        except OSError:
            continue
        items.extend(_load_items(path))
    return items


def _load_items(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix == ".jsonl":
        items: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                items.append(loaded)
        return items

    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    return [loaded] if isinstance(loaded, dict) else []


def _index_comments(
    comments: list[dict[str, Any]], ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """把评论按归属内容 id 归拢。

    不同平台的归属字段名不同（note_id / aweme_id / video_id / content_id），
    因此用内容 id 集合去反查评论里的同名字段，而不是按平台写分支。
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        for key in _ID_KEYS:
            value = str(comment.get(key) or "").strip()
            if value and value in ids:
                index.setdefault(value, []).append(comment)
                break
    return index


def _to_doc(
    *,
    platform_key: str,
    label: str,
    item: dict[str, Any],
    comments: list[dict[str, Any]],
) -> Doc:
    content_id = str(_pick(item, _ID_KEYS) or "").strip()
    url = str(_pick(item, _URL_KEYS) or "").strip() or _template_url(platform_key, content_id)
    title = _clip(str(_pick(item, _TITLE_KEYS) or "").strip(), 120)
    desc = str(_pick(item, _DESC_KEYS) or "")
    likes = _to_number(_pick(item, _LIKE_KEYS))

    return Doc(
        title=title or f"{label}内容 {content_id or '(无 ID)'}",
        url=url,
        snippet=_clip(desc, 200),
        content=_clip(desc, 3000),
        source=platform_key,
        source_kind=source_kind_value(SourceKind.SOCIAL_CN),
        platform=label,
        author=str(_pick(item, _AUTHOR_KEYS) or ""),
        published_at=str(_pick(item, _TIME_KEYS) or ""),
        score=likes,
        extras={
            "crawler": "mediacrawler",
            "content_id": content_id,
            "likes": likes,
            "hot_comments": _hot_comments(comments),
        },
    )


def _hot_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按点赞降序取 top3（SPEC 4.7）。

    昵称在 MediaCrawler 侧已脱敏，这里只做取值与排序，不补任何造数据。
    """
    ranked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for comment in comments:
        text = _clip(str(_pick(comment, _COMMENT_TEXT_KEYS) or "").strip(), 200)
        if not text:
            continue
        nickname = str(comment.get("nickname") or comment.get("user_nickname") or "")
        # MediaCrawler 会按日期文件反复追加，同一条评论可能被写进多次。
        # 不去重的话，一条热评会以"三条热评"的样子出现在证据里，等于虚增证据。
        key = (nickname, text)
        if key in seen:
            continue
        seen.add(key)
        ranked.append(
            {
                "nickname": nickname,
                "content": text,
                "likes": _to_number(_pick(comment, _LIKE_KEYS)),
            }
        )
    ranked.sort(key=lambda entry: entry["likes"], reverse=True)
    return ranked[:_MAX_HOT_COMMENTS]


def _template_url(platform_key: str, content_id: str) -> str:
    if not content_id:
        return ""
    if platform_key == "bilibili":
        # B 站的 video_id 可能是纯数字 aid，必须带 av 前缀，否则 404
        prefix = "av" if content_id.isdigit() else ""
        return f"https://www.bilibili.com/video/{prefix}{content_id}"
    template = _URL_TEMPLATES.get(platform_key)
    return template.format(content_id) if template else ""


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        if value == 0 and key in _TIME_KEYS:
            continue
        return value
    return None


def _to_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clip(text: str, limit: int) -> str:
    """超长内容先截断再进上下文（SPEC 4.5e）。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"
