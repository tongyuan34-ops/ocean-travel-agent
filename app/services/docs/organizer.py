"""文档整理：扫描 → 分类 → 出计划 → 执行。

**为什么分成「计划」和「执行」两步**

整理是不可逆的批量操作：一次移动五十个文件，如果分类规则写错了，用户面对的是
五十个不在原位的文件，而且他多半记不住哪个原来在哪。所以这里默认只**出计划**，
把「哪个文件要移到哪」逐条列出来给用户看；他点头之后，模型再调一次带
``apply=True`` 的执行。计划本身也够有用——用户可以直接照着看分类对不对。

**为什么按文件名分类，不看内容**

看内容要逐份解析，五十个文件就是五十次 IO + 解析，慢且没必要：文件名是用户自己
起的，合同叫「XX合同」、报价单叫「XX报价单」，这个信号已经足够。真需要按内容
分类时，先 ``digest_documents`` 看一眼再让模型给规则，不要在这一层做猜测。

**为什么不删任何东西**

整理只做「移动」和「重命名」，不删、不覆盖。目标位置有同名文件时自动加序号，
所以最坏结果是多出几个带序号的副本，而不是丢东西。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.services.docs.reader import SUPPORTED_SUFFIXES, DocError

# 一次最多处理多少个文件。再多就该分批了——用户看不过来，出错也难核对
MAX_FILES = 200

# Windows 文件名禁用字符。分类目录名和重命名后的文件名都要过这一层，
# 否则一个带「/」的分类名就能把文件写到目录外面去
_ILLEGAL = '<>:"/\\|?*'


def _safe_name(name: str, *, fallback: str = "未分类") -> str:
    cleaned = "".join("_" if ch in _ILLEGAL or ord(ch) < 32 else ch for ch in name).strip()
    cleaned = cleaned.strip(". ")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:60]


@dataclass(frozen=True)
class Category:
    """一条分类规则：命中任一关键词就归到 ``name``。"""

    name: str
    keywords: tuple[str, ...]

    def matches(self, filename: str) -> bool:
        lowered = filename.lower()
        return any(keyword.lower() in lowered for keyword in self.keywords)


@dataclass(frozen=True)
class Move:
    src: Path
    dst: Path
    category: str


@dataclass
class OrganizePlan:
    root: Path
    moves: list[Move] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.moves

    def to_text(self, *, applied: bool = False) -> str:
        if self.empty:
            lines = [f"{self.root} 里没有需要整理的文件。"]
            if self.skipped:
                lines.append("跳过：")
                lines.extend(f"- {name}：{reason}" for name, reason in self.skipped[:20])
            return "\n".join(lines)

        head = (
            f"已整理 {len(self.moves)} 个文件："
            if applied
            else f"整理计划（共 {len(self.moves)} 个文件，**还没有动任何文件**）："
        )
        lines = [head]
        for move in self.moves:
            try:
                relative = move.dst.relative_to(self.root)
            except ValueError:  # pragma: no cover - dst 一定在 root 下
                relative = move.dst
            lines.append(f"- {move.src.name} → {relative}")
        if self.skipped:
            lines.append("")
            lines.append("跳过：")
            lines.extend(f"- {name}：{reason}" for name, reason in self.skipped[:20])
        if not applied:
            lines.append("")
            lines.append("确认无误后，用 apply=True 再调一次才会真正移动。")
        return "\n".join(lines)


def parse_categories(raw: list[dict[str, object]]) -> list[Category]:
    """把模型给的 ``[{"name": "合同", "keywords": ["合同", "协议"]}]`` 变成规则。

    没有关键词的分类直接丢掉：它的 ``matches`` 永远为假，留着只会让模型以为
    「已经配了这个分类」，然后奇怪为什么没有文件进去。
    """
    categories: list[Category] = []
    for item in raw:
        name = _safe_name(str(item.get("name") or ""))
        keywords = item.get("keywords")
        words = tuple(str(word).strip() for word in keywords) if isinstance(keywords, list) else ()
        words = tuple(word for word in words if word)
        if not words:
            continue
        categories.append(Category(name=name, keywords=words))
    if not categories:
        raise DocError("没有有效的分类规则：每个分类至少要有一个关键词")
    return categories


def _dedupe(target: Path) -> Path:
    """目标已存在时加序号，绝不覆盖。"""
    if not target.exists():
        return target
    index = 2
    while True:
        candidate = target.with_name(f"{target.stem}（{index}）{target.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _render_name(pattern: str, *, title: str, category: str, mtime: float, index: int) -> str:
    date = time.strftime("%Y-%m-%d", time.localtime(mtime))
    rendered = (
        pattern.replace("{date}", date)
        .replace("{title}", title)
        .replace("{category}", category)
        .replace("{index}", str(index))
    )
    return _safe_name(rendered, fallback=title)


def build_plan(
    root: Path,
    categories: list[Category],
    *,
    rename_pattern: str = "",
    recursive: bool = True,
    limit: int = MAX_FILES,
) -> OrganizePlan:
    """扫描目录并生成计划。**不动任何文件**。"""
    if not root.is_dir():
        raise DocError(f"不是目录：{root}")

    category_names = {category.name for category in categories}
    pattern = "**/*" if recursive else "*"
    plan = OrganizePlan(root=root)

    candidates = [
        item
        for item in sorted(root.glob(pattern))
        if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
    ]
    if len(candidates) > limit:
        plan.skipped.append((f"其余 {len(candidates) - limit} 个文件", f"一次最多处理 {limit} 个"))
        candidates = candidates[:limit]

    for index, item in enumerate(candidates, start=1):
        # 已经在分类目录里的不再处理，否则第二次整理会把它再挪一层
        if item.parent != root and item.parent.name in category_names:
            continue

        matched = next((category for category in categories if category.matches(item.name)), None)
        if matched is None:
            plan.skipped.append((item.name, "没命中任何分类关键词"))
            continue

        stem = item.stem
        if rename_pattern:
            stem = _render_name(
                rename_pattern,
                title=item.stem,
                category=matched.name,
                mtime=item.stat().st_mtime,
                index=index,
            )
        target = _dedupe(root / matched.name / f"{stem}{item.suffix}")
        plan.moves.append(Move(src=item, dst=target, category=matched.name))

    return plan


def apply_plan(plan: OrganizePlan) -> list[Move]:
    """执行计划。返回真正移动成功的项（失败的不在结果里，并抛 ``DocError``）。"""
    if plan.empty:
        raise DocError("计划是空的，没有可执行的动作")

    done: list[Move] = []
    for move in plan.moves:
        if not move.src.exists():
            raise DocError(f"文件已经不在原位：{move.src.name}")
        try:
            move.dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.src), str(move.dst))
        except OSError as exc:
            raise DocError(f"移动 {move.src.name} 失败：{exc}") from exc
        done.append(move)
    return done
