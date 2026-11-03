"""knowledge 组工具：检索知识库里的规则与判断标准（RAG）。

**为什么是工具，而不是每轮自动检索**

自动检索（把 top-k 拼进 system prompt）看起来更「智能」，但代价是每轮固定多花
几百 token，而且检索不准时会**污染**这一轮的全部回答——用户问「你好」也带上
三条行程规则，模型反而容易跑题。

做成工具的好处是：模型自己判断要不要查、查什么词，调用过程在 SSE 里可见
（``tool_call`` / ``tool_result``），出问题能复盘是哪一步检索错了。
代价是模型可能不查——所以提示词里有一条硬规则要求「涉及节奏、预算口径、
带老人小孩、轮渡与天气应对时，先查知识库」。

**输出契约**

首行给统计（命中条数 / 语料规模），后面每条带 ``[来源]``，
最后一行是引用要求。与 search 组工具保持同样的形状——
前端和模型都不用为「另一种检索结果」写特例。
"""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.config import load_settings
from app.core.context import truncate_tool_output
from app.rag import get_knowledge_base

_MAX_LIMIT = 6
_LIST_LIMIT = 2400


class SearchKnowledgeInput(BaseModel):
    query: str = Field(description="要查的主题或问题，如「带两岁小孩的行程节奏」「预算怎么分项」")
    limit: int = Field(default=3, description="返回条数上限，1..6")


@tool(args_schema=SearchKnowledgeInput)
async def search_knowledge(query: str, limit: int = 3) -> str:
    """检索行程规划规则库：节奏安排、预算口径、亲子与长辈出行、海岛轮渡、雨天备选。

    只包含**稳定的规则与判断标准**，不含班次、票价、天气这类实时事实——
    那些必须用 plan_route / get_weather / search_web 去查。
    """
    query = (query or "").strip()
    if not query:
        return "INVALID_REQUEST：检索主题不能为空"

    settings = load_settings()
    base = get_knowledge_base(settings.knowledge_dir)
    if base.size == 0:
        return (
            "KNOWLEDGE_EMPTY：知识库为空（未找到语料文件）。"
            "请按通用经验回答，并说明这些是经验规则而不是项目知识库的内容。"
        )

    limit = max(1, min(int(limit), _MAX_LIMIT))
    hits = base.search(query, limit)

    if not hits:
        return (
            f"KNOWLEDGE_EMPTY：知识库里没有与「{query}」相关的条目（共 {base.size} 个片段）。"
            "不要编造规则；可以按通用经验回答，但要说明这不是项目知识库的内容。"
        )

    lines = [f"知识库检索「{query}」（语料 {base.size} 个片段，命中 {len(hits)} 条）", ""]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"[{index}] {hit.chunk.citation}")
        lines.append(hit.chunk.text)
        lines.append("")

    lines.append("引用要求：以上是项目知识库里的规则，回答时可以据此判断，")
    lines.append("但涉及班次、票价、天气等实时信息，必须另用工具查询，不得用规则替代。")
    return truncate_tool_output("\n".join(lines), limit=_LIST_LIMIT)
