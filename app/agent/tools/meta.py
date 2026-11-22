"""元工具：申请能力。

**它解决的问题**

工具是按轮路由的（见 ``app/agent/toolset.py``），所以模型手上永远只有一小撮
工具。关键词路由必然有漏网的时候——用户说「帮我把这些东西归归类」，
没沾到 docs 组的任何一个词，模型就会回答「我没有整理文件的工具」。

这一层把「没有工具」变成「可以申请」：模型描述要做什么，这里把命中的组
**激活到会话**，下一轮这些工具就真的出现在它的工具列表里。

**为什么激活而不是直接返回工具**

LangChain 的 ``bind_tools`` 是构图时固定的，运行中给模型「塞一个工具」没有
对应机制。激活 + 下一轮重绑是这套架构里唯一不需要额外循环的做法。
代价是多一轮，所以返回文本里明确要求模型：这轮先如实说「我在准备」，
**不要假装已经完成**。
"""

from __future__ import annotations

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.toolset import activate, describe, route


class DiscoverInput(BaseModel):
    need: str = Field(
        default="",
        description="你要做的事，一句中文即可；留空只列能力清单",
    )


@tool(args_schema=DiscoverInput)
def discover_tools(need: str = "") -> str:
    """申请更多工具。

    你的工具列表按本轮话题挑过，不是全集。要读文档、算表格、生图、检索网页、
    操作本机文件却没有对应工具时，**先调用本工具**，不要直接说「做不到」。

    命中的那几组从**下一轮**起可用；这轮如实说「我在准备」，不要假装已完成。
    """
    text = (need or "").strip()
    if not text:
        return "全部可用能力如下（未激活任何工具）：\n" + describe()

    keys = route(text)
    if not keys:
        return (
            "没有匹配到具体能力组。请把你打算做的事说得更具体一些"
            "（例如「读 Excel 并按金额排序」「查三亚的天气」）。\n\n"
            "全部能力：\n" + describe()
        )

    added = activate(keys)
    body = describe(keys)
    if added:
        head = f"已激活：{'、'.join(added)}。这些工具从下一轮起可用。"
    else:
        head = f"这些组本来就可用：{'、'.join(keys)}。直接用即可，不必再申请。"
    return f"{head}\n\n{body}"


__all__ = ["DiscoverInput", "discover_tools"]
