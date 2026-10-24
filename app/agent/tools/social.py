"""plugin 组 B 工具：登录态社媒采集（默认关闭、个人自用、不随核心开源发布）。

对外只暴露两个工具（SPEC 4.6）：
- `search_social_session(query, platforms, limit)`：真跑 MediaCrawler 子进程采集
- `generate_scraper(site, goal)`：**只生成脚本草案，绝不自动执行**

安全底线：
- `ENABLE_SESSION_FETCH=false` 时直接返回 TOOL_NOT_ENABLED，不做任何尝试
- 未配置 Cookie 的平台一律跳过，并逐条说明原因，绝不"带着未登录态硬跑"
- 输出不包含任何 Cookie 内容
"""

from __future__ import annotations

import time

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.adapters import find, social_adapters
from app.adapters.mediacrawler import MediaCrawlerAdapter
from app.core.config import PROJECT_ROOT, load_settings
from app.core.context import truncate_tool_output
from app.core.models import Doc

_MAX_LIMIT = 10
_LIST_LIMIT = 3000


class SearchSocialInput(BaseModel):
    query: str = Field(description="检索关键词，如「三亚 亚龙湾 攻略」")
    platforms: list[str] = Field(
        default_factory=list,
        description="要检索的平台名列表，如 ['douyin','xiaohongshu']；留空表示所有已配置的平台",
    )
    limit: int = Field(default=5, description="每个平台返回条数上限，1..10")


@tool(args_schema=SearchSocialInput)
async def search_social_session(
    query: str, platforms: list[str] | None = None, limit: int = 5
) -> str:
    """在**已配置登录态 Cookie** 的社媒平台按关键词检索内容（默认关闭的插件）。

    只在用户明确要求检索社媒内容时调用。采集耗时长且存在平台风控风险，
    未配置 Cookie 的平台会被跳过并说明原因。
    """
    settings = load_settings()
    if not settings.enable_session_fetch:
        return (
            "TOOL_NOT_ENABLED：登录态社媒采集默认关闭（.env 的 ENABLE_SESSION_FETCH=false）。"
            "该功能个人自用、不随核心开源发布，开启前请自行评估平台服务条款与封号风险"
        )

    query = (query or "").strip()
    if not query:
        return "INVALID_REQUEST：检索关键词不能为空"

    requested = [name.strip().lower() for name in (platforms or []) if name and name.strip()]
    candidates = social_adapters()
    if requested:
        wanted = set(requested)
        candidates = [item for item in candidates if item.name in wanted]
        unknown = wanted - {item.name for item in candidates}
        if unknown:
            return (
                f"INVALID_REQUEST：不支持的平台：{'、'.join(sorted(unknown))}。"
                f"可用平台：{'、'.join(item.name for item in social_adapters())}"
            )

    limit = max(1, min(limit, _MAX_LIMIT))
    ready = [item for item in candidates if item.available()]
    blocked = [item for item in candidates if not item.available()]

    if not ready:
        detail = "；".join(f"{item.name}（{item.unavailable_reason()}）" for item in blocked)
        return f"TOOL_NOT_ENABLED：没有可用的社媒平台。{detail or '请在 .env 中开启 ENABLE_SESSION_FETCH'}"

    started = time.perf_counter()
    docs_by_platform: dict[str, list[Doc]] = {}
    failed: dict[str, str] = {}
    degraded = False

    # 串行执行：并发跑多个平台的采集等于对平台施加成倍请求，风控风险显著上升
    for adapter in ready:
        try:
            docs = await adapter.search(query, limit)
        except Exception as exc:  # noqa: BLE001 - 单源失败不得拖垮整体
            failed[adapter.name] = str(exc)
            continue
        if docs and all(doc.mock for doc in docs):
            degraded = True
            failed[adapter.name] = str(docs[0].extras.get("reason") or "源不可用")
            continue
        docs_by_platform[adapter.name] = docs

    elapsed = time.perf_counter() - started
    hit_sources = sum(1 for docs in docs_by_platform.values() if docs)
    counts = "、".join(f"{name}={len(docs)}" for name, docs in docs_by_platform.items())
    real_docs = [doc for docs in docs_by_platform.values() for doc in docs]

    lines = [f"社媒检索「{query}」（登录态采集，仅覆盖已配置 Cookie 的平台）"]
    lines.append(
        f"统计：命中源数 {hit_sources}｜各源命中数 {counts or '无'}｜"
        f"耗时 {elapsed:.1f}s｜降级 {'是' if degraded or failed else '否'}"
    )

    if not real_docs:
        lines.append("")
        lines.append("⚠️ 本次未获得任何真实结果。")
        for name, reason in failed.items():
            lines.append(f"- {name} 失败：{reason}")
        lines.append("请如实告知用户本次采集失败，不要用常识补足内容。")
        return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)

    for docs in docs_by_platform.values():
        if not docs:
            continue
        lines.append("")
        lines.append(f"【{docs[0].platform}】（{len(docs)} 条）")
        for index, doc in enumerate(docs, start=1):
            lines.append(f"{index}. {doc.title}")
            if doc.url:
                lines.append(f"   链接：{doc.url}")
            meta = []
            if doc.author:
                meta.append(f"作者 {doc.author}")
            if doc.score:
                meta.append(f"点赞 {int(doc.score)}")
            if doc.published_at:
                meta.append(f"时间 {doc.published_at}")
            if meta:
                lines.append(f"   {' ｜ '.join(meta)}")
            if doc.snippet:
                lines.append(f"   正文：{doc.snippet}")
            for comment in doc.extras.get("hot_comments") or []:
                lines.append(
                    f"   热门评论：{comment.get('nickname') or '匿名'}"
                    f"（赞 {int(comment.get('likes') or 0)}）：{comment.get('content')}"
                )

    if failed:
        lines.append("")
        lines.append("未完成的平台：")
        for name, reason in failed.items():
            lines.append(f"- {name}：{reason}")
    if blocked:
        lines.append("")
        lines.append("未就绪的平台：")
        for adapter in blocked:
            lines.append(f"- {adapter.name}：{adapter.unavailable_reason()}")

    lines.append("")
    lines.append("说明：以上内容来自你登录态下的抓取结果，不代表平台全量数据；")
    lines.append("引用时请标注平台与链接，并遵守各平台服务条款、控制请求频率。")
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)


class GenerateScraperInput(BaseModel):
    site: str = Field(
        description="目标平台，如 douyin / xiaohongshu / bilibili / weibo / zhihu / tieba"
    )
    goal: str = Field(description="想采集什么，例如「三亚旅游攻略图文与高赞评论」")


@tool(args_schema=GenerateScraperInput)
async def generate_scraper(site: str, goal: str) -> str:
    """生成一份 MediaCrawler 采集脚本草案（只输出文本，**绝不以任何方式自动执行**）。

    用于让用户先复核逻辑与合规性，再自行决定是否运行。
    """
    key = (site or "").strip().lower()
    matched = find(key)
    # 只有 MediaCrawlerAdapter 才带 crawler_code。名字匹配之外再做一次类型收窄，
    # 否则接新的社媒适配器时这里会在运行期抛 AttributeError
    if not isinstance(matched, MediaCrawlerAdapter) or key not in {
        item.name for item in social_adapters()
    }:
        return (
            f"INVALID_REQUEST：不支持的平台：{site or '（空）'}。"
            f"可用平台：{'、'.join(item.name for item in social_adapters())}"
        )
    goal = (goal or "").strip() or "（未描述目标）"

    settings = load_settings()
    python_path = f"{settings.mediacrawler_path}/.venv/Scripts/python.exe"
    code = matched.crawler_code
    label = matched.platform
    data_dir = PROJECT_ROOT / "data"

    lines = [
        f"采集脚本草案（{label} / {matched.name}）",
        "⚠️ 本工具只生成草案文本，不会执行任何采集动作；请在复核后再自行运行。",
        "",
        f"目标：{goal}",
        "",
        "一、前置条件",
        f"1. MediaCrawler 已部署在 {settings.mediacrawler_path}，其虚拟环境可用",
        "2. 已在 config/base_config.py 中设置 ENABLE_CDP_MODE=False、CDP_CONNECT_EXISTING=False",
        f"3. 已用 scripts/apply_cookies.py 写入 {matched.name} 的登录态 Cookie",
        "4. 请遵守平台服务条款：控制频率与数量，不做批量或商业用途采集",
        "",
        "二、可直接运行的命令",
        (
            f'"{python_path}" main.py --platform {code} --lt cookie --type search '
            f'--keywords "{goal}" --save_data_option json '
            f'--save_data_path "{data_dir}" --crawler_max_notes_count 10 '
            f"--max_comments_count_singlenotes 3 --get_comment yes --get_sub_comment no "
            f"--get_media no --headless yes"
        ),
        "",
        f"三、脚本草案（另存为 run_{code}.py 后按需修改）",
        "```python",
        '"""按关键词采集 %s 内容与高赞评论。运行前请确认 Cookie 与合规性。"""' % label,
        "import subprocess, sys",
        "from pathlib import Path",
        "",
        f'MEDIACRAWLER = Path(r"{settings.mediacrawler_path}")',
        f'SAVE_DATA = Path(r"{data_dir}")',
        f'COOKIES = (Path(r"{PROJECT_ROOT}") / "cookies" / "{matched.name}.txt").read_text(encoding="utf-8").strip()',
        "",
        "def main(keyword: str, limit: int = 10) -> int:",
        "    cmd = [",
        '        str(MEDIACRAWLER / ".venv" / "Scripts" / "python.exe"),',
        '        str(MEDIACRAWLER / "main.py"),',
        f'        "--platform", "{code}", "--lt", "cookie", "--type", "search",',
        '        "--keywords", keyword, "--cookies", COOKIES,',
        '        "--save_data_option", "json", "--save_data_path", str(SAVE_DATA),',
        '        "--crawler_max_notes_count", str(limit),',
        '        "--max_comments_count_singlenotes", "3",',
        '        "--get_comment", "yes", "--get_sub_comment", "no",',
        '        "--get_media", "no", "--headless", "yes",',
        "    ]",
        "    return subprocess.run(cmd, cwd=str(MEDIACRAWLER)).returncode",
        "",
        'if __name__ == "__main__":',
        '    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "%s"))' % goal,
        "```",
        "",
        "四、结果位置",
        f"- 内容：{data_dir}/{code}/json/search_contents_<日期>.json",
        f"- 评论：{data_dir}/{code}/json/search_comments_<日期>.json",
        "",
        "五、提醒",
        "- 关键词请按平台习惯调整，草案里的关键词直接取自你的目标描述",
        "- 采集频率与数量由你控制；高频请求会被风控，也可能导致账号受限",
    ]
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)
