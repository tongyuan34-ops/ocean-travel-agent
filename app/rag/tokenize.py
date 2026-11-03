"""中文友好的分词：不做词性标注，只产出 BM25 能用的 term。

**为什么不用 jieba**

jieba 是 30MB+ 带词典的依赖，而且首次加载要预热。这个项目的知识库是几十篇短文档，
BM25 的检索质量主要取决于「term 是否稳定」，不取决于「切词是否语言学正确」。
中文用**二元组（bigram）**是搜索引擎里的标准做法：把「湄洲岛」切成「湄洲」「洲岛」，
虽然多了一个假词，但保证了任何两字组合都能被命中，召回比精确切词更稳。

代价说清楚：单字查询（比如只搜「船」）在纯二元组下命中很差。所以长度 1 的
连续中文串会原样保留成一个 term，作为兜底。

英文和数字按「连续 ASCII 字母数字」切，统一小写。全角标点、emoji 一律当分隔符。
"""

from __future__ import annotations

import re
import unicodedata

# 连续的 ASCII 字母数字，允许中间带 _ 或 -（如 gpt-4、v2_1）
_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]*")
# CJK 统一表意文字（含扩展 A）与兼容区
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# 中文虚词与套话。小语料里这些词的 IDF 反而很高（因为出现文档少），
# 会把真正的主题词压下去：搜「预算怎么分项」时，「怎么」赢过「预算」。
# 只收高置信度的功能词，宁可少收——多收会误伤「怎么做预算」这类正常表达。
STOPWORDS: frozenset[str] = frozenset(
    {
        "怎么",
        "什么",
        "为什",
        "如何",
        "哪些",
        "哪个",
        "多少",
        "是否",
        "是不",
        "不是",
        "可以",
        "需要",
        "应该",
        "一般",
        "一下",
        "一些",
        "这个",
        "那个",
        "这些",
        "那些",
        "我们",
        "你们",
        "他们",
        "自己",
        "的话",
        "时候",
        "现在",
        "然后",
        "还是",
        "或者",
        "因为",
        "所以",
        "但是",
        "如果",
        "而且",
        "就是",
        "已经",
        "没有",
        "还有",
        "以及",
        "一个",
        "一种",
        "这样",
        "那样",
        "怎样",
        "有没",
        "能不",
        "不能",
    }
)


def normalize(text: str) -> str:
    """全角转半角、统一小写。不做繁简转换——那需要额外的表。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def tokenize(text: str) -> list[str]:
    """切成 term 列表，保留重复（BM25 需要词频）。

    先按「CJK 段 / 非 CJK 段」切开，再分别处理：
    - CJK 段：长度 1 原样，长度 ≥2 取所有相邻二元组
    - 非 CJK 段：按 ASCII 词切
    """
    normalized = normalize(text)
    tokens: list[str] = []
    buffer: list[str] = []
    is_cjk: bool | None = None

    def flush() -> None:
        if not buffer:
            return
        chunk = "".join(buffer)
        if is_cjk:
            if len(chunk) == 1:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        else:
            tokens.extend(_ASCII_WORD.findall(chunk))
        buffer.clear()

    for char in normalized:
        if _CJK.match(char):
            if is_cjk is False:
                flush()
            is_cjk = True
            buffer.append(char)
        elif char.isalnum() or (char in "-_" and is_cjk is False):
            # 连字符和下划线属于 ASCII 词的一部分（gpt-4 / v2_1），
            # 但只在前一个字符也是 ASCII 词时才并入，避免「预算-轮渡」粘成一个词
            if is_cjk is True:
                flush()
            is_cjk = False
            buffer.append(char)
        else:
            # 标点、空白、emoji：分隔符
            flush()
            is_cjk = None
    flush()
    return [token for token in tokens if token not in STOPWORDS]
