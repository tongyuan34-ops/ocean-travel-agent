"""search_knowledge 工具：输出契约与边界。

这个工具最容易出的问题不是检索不准，而是**它被当成了事实来源**。
知识库里只有规则，班次票价必须另查——所以用例要盯住这句约束还在不在。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.agent.tools import knowledge as knowledge_module
from app.agent.tools.knowledge import search_knowledge
from app.rag import reset_cache

KNOWLEDGE_DIR = Path(__file__).resolve().parents[1] / "knowledge"


def _point_at(monkeypatch, directory: Path) -> None:
    """把工具读的目录换成临时目录。

    必须先抓住原函数再替换——直接调 ``knowledge_module.load_settings()``
    会调到自己刚打上的补丁，递归爆栈。
    """
    original = knowledge_module.load_settings
    monkeypatch.setattr(
        knowledge_module,
        "load_settings",
        lambda: replace(original(), knowledge_dir=str(directory)),
    )
    reset_cache()


async def test_empty_query_is_rejected():
    output = await search_knowledge.ainvoke({"query": "  "})

    assert output.startswith("INVALID_REQUEST")


async def test_empty_corpus_says_so_instead_of_inventing(tmp_path: Path, monkeypatch):
    """语料缺失时必须明说，并允许模型退回通用经验——但不能装作查到了。"""
    _point_at(monkeypatch, tmp_path)

    output = await search_knowledge.ainvoke({"query": "预算怎么分项"})

    assert output.startswith("KNOWLEDGE_EMPTY")


async def test_relative_knowledge_dir_does_not_follow_cwd(tmp_path: Path, monkeypatch):
    """默认配置 ``KNOWLEDGE_DIR=knowledge`` 是相对路径，不能跟着进程 CWD 走。

    打包后双击 exe，CWD 是 exe 所在目录，而语料在 ``_internal/`` 里——解析
    跟着 CWD 走的结果就是「语料明明打进了包，运行时一个片段都读不到」，
    界面上显示「检索失败」，日志里只有一句目录不存在的 warning。
    """
    _point_at(monkeypatch, Path("knowledge"))
    monkeypatch.chdir(tmp_path)

    output = await search_knowledge.ainvoke({"query": "预算怎么分项"})

    assert "KNOWLEDGE_EMPTY" not in output
    assert "20-预算口径.md" in output


async def test_hits_carry_citation_and_content(monkeypatch):
    _point_at(monkeypatch, KNOWLEDGE_DIR)

    output = await search_knowledge.ainvoke({"query": "预算怎么分项", "limit": 2})

    assert "20-预算口径.md" in output
    assert "›" in output  # 来源 › 小节
    assert "交通" in output
    # 输出必须提醒「实时事实另查」，否则模型会拿规则当票价
    assert "必须另用工具查询" in output


async def test_limit_is_clamped(monkeypatch):
    _point_at(monkeypatch, KNOWLEDGE_DIR)

    output = await search_knowledge.ainvoke({"query": "行程", "limit": 99})

    assert output.count("[") <= 7  # 6 条上限 + 首行统计


async def test_tool_description_states_the_boundary():
    """描述是模型唯一能看到的地方，边界必须写在这里。

    只写在知识库文件里没用——模型检索之前看不到那些文件。
    """
    description = search_knowledge.description or ""

    assert "规则" in description
    assert "实时事实" in description
