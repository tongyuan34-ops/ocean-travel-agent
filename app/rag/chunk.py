"""把 Markdown 切成检索单元。

**为什么按标题切，而不是定长切**

定长切（每 500 字一刀）会把「亲子出行的三条注意事项」拦腰截断，检索命中的那一段
只有半条规则，模型读到的就是残缺的。按标题切保证每个块是**一个完整语义单元**，
而标题本身作为元数据留在块上——检索结果里带上「来自《亲子出行》· 雨天备选」，
用户能判断这条规则是否适用于自己的情况。

超长小节再按段落聚合到 ``max_chars`` 左右，段落之间留 ``overlap`` 的重叠：
一句话跨段的情况在中文里很常见，不重叠会丢。

块 id 用 ``文件名#序号``，稳定可复现——同一份语料每次构建出来的 id 必须一样，
否则引用就无法回溯（这和「生成的东西要能验收」是同一条要求）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 一级到六级标题；标题行本身不进正文，进元数据
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

DEFAULT_MAX_CHARS = 420
DEFAULT_OVERLAP = 80


@dataclass(frozen=True)
class Chunk:
    """一个检索单元。``id`` 稳定，``heading`` 用于引用展示。"""

    id: str
    source: str
    heading: str
    text: str

    @property
    def citation(self) -> str:
        return f"{self.source} › {self.heading}" if self.heading else self.source


def _split_sections(text: str) -> list[tuple[str, str]]:
    """按标题切段，返回 (标题, 正文)。一级标题之前的内容标题为空。"""
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []

    for line in text.splitlines():
        match = _HEADING.match(line.strip())
        if match:
            if heading or any(part.strip() for part in body):
                sections.append((heading, "\n".join(body).strip()))
            heading = match.group(2).strip()
            body = []
            continue
        body.append(line)

    if heading or any(part.strip() for part in body):
        sections.append((heading, "\n".join(body).strip()))
    return [(title, content) for title, content in sections if content]


def _paragraphs(body: str) -> list[str]:
    parts = re.split(r"\n\s*\n", body)
    return [part.strip() for part in parts if part.strip()]


def _pack(paragraphs: list[str], max_chars: int, overlap: int) -> list[str]:
    """把段落聚成不超过 ``max_chars`` 的块，块之间共享尾部 ``overlap`` 字。"""
    blocks: list[str] = []
    current: list[str] = []
    length = 0

    for paragraph in paragraphs:
        if current and length + len(paragraph) + 2 > max_chars:
            blocks.append("\n\n".join(current))
            tail = blocks[-1]
            carry = tail[-overlap:] if overlap > 0 and len(tail) > overlap else tail
            current = [carry] if carry else []
            length = len(carry)
        current.append(paragraph)
        length += len(paragraph) + 2

    if current:
        blocks.append("\n\n".join(current))
    return [block for block in blocks if block.strip()]


def split_markdown(
    source: str,
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """把一份文档切成若干 ``Chunk``。序号按出现顺序，从 0 开始。"""
    chunks: list[Chunk] = []
    for heading, body in _split_sections(text):
        for block in _pack(_paragraphs(body), max_chars, overlap):
            chunks.append(
                Chunk(
                    id=f"{source}#{len(chunks)}",
                    source=source,
                    heading=heading,
                    text=block,
                )
            )
    return chunks
