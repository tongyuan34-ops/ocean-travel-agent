"""agent 图：native 与 structured 两条路径都要能完成多步工具调用（SPEC 4.4）。

模型换成可脚本化的假模型、工具换成桩，
校验的是"两条路径都能跑通"，不是模型能力或外部 API。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool as lc_tool
from pydantic import Field

from app.agent import graph as graph_module


@lc_tool
def echo(text: str) -> str:
    """回显输入的文本，用于验证工具调用链路。"""
    return f"echo:{text}"


class ScriptedChatModel(BaseChatModel):
    """按脚本依次吐出预设消息，并记录每次收到的是什么。"""

    responses: list[Any] = Field(default_factory=list)
    cursor: int = 0
    bound_tools: list[Any] = Field(default_factory=list)
    seen: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        self.bound_tools = list(tools)
        return self

    def _take(self, messages) -> AIMessage:
        self.seen.extend(str(getattr(item, "content", "")) for item in messages)
        message = self.responses[min(self.cursor, len(self.responses) - 1)]
        self.cursor += 1
        return message

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._take(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._take(messages))])


def _patch(monkeypatch, responses):
    model = ScriptedChatModel(responses=responses)
    monkeypatch.setattr(graph_module, "build_chat_model", lambda _s: model)
    return model


async def test_native_mode_binds_tools_and_executes(monkeypatch, settings):
    model = _patch(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "c1"}],
            ),
            AIMessage(content="完成"),
        ],
    )
    graph, mode = graph_module.build_agent_graph(settings, mode="native", tools=[echo])
    assert mode == "native"
    assert [tool.name for tool in model.bound_tools] == ["echo"]

    result = await graph.ainvoke({"messages": [HumanMessage(content="调用工具")]})
    contents = [getattr(message, "content", "") for message in result["messages"]]

    assert "完成" in contents  # 拿到最终回答
    assert "echo:hi" in contents  # 工具真的被执行过


async def test_structured_mode_decides_via_json_then_executes(monkeypatch, settings):
    """不支持原生 tool calling 的模型走「JSON 决策 + 代码执行」这条回退路径。"""
    model = _patch(
        monkeypatch,
        [
            AIMessage(content='{"tool": "echo", "args": {"text": "hi"}}'),
            AIMessage(content='{"final": "完成"}'),
        ],
    )
    graph, mode = graph_module.build_agent_graph(settings, mode="structured", tools=[echo])
    assert mode == "structured"

    result = await graph.ainvoke({"messages": [HumanMessage(content="调用工具")]})
    contents = [getattr(message, "content", "") for message in result["messages"]]

    assert "完成" in contents
    # 工具确实被执行过：结构化模式不把中间结果写进图状态，
    # 因此从"模型第二次收到的输入里带回显结果"来验证
    assert any("echo:hi" in text for text in model.seen)
    assert model.cursor >= 2


async def test_structured_mode_handles_unknown_tool(monkeypatch, settings):
    """模型编了个不存在的工具时，要如实说明而不是崩掉。"""
    _patch(monkeypatch, [AIMessage(content='{"tool": "no_such_tool", "args": {}}')])
    graph, _ = graph_module.build_agent_graph(settings, mode="structured", tools=[echo])
    result = await graph.ainvoke({"messages": [HumanMessage(content="调用工具")]})
    contents = [getattr(message, "content", "") for message in result["messages"]]
    assert any("不存在" in text for text in contents)


def test_parse_decision_tolerates_surrounding_text():
    from app.agent.graph import _parse_decision

    assert _parse_decision('说明：{"final": "好的"} 完毕') == {"final": "好的"}
    assert _parse_decision("完全不是 JSON") is None
    assert _parse_decision("[1, 2]") is None


# ---------- 工具崩溃兜底 ----------


@lc_tool
async def boom(text: str) -> str:
    """故意抛异常的工具，用于验证崩溃兜底。"""
    raise TypeError(f"炸了：{text}")


def test_handle_tool_error_turns_crash_into_text():
    """未预期异常要变成错误文本，而不是让整轮对话 500。"""
    from app.agent.graph import _handle_tool_error

    result = _handle_tool_error(TypeError("boom"))
    assert result.startswith("TOOL_CRASHED")
    assert "TypeError" in result


def test_handle_tool_error_keeps_invocation_message():
    """参数不合法是模型的问题，原文回给它最有信息量。"""
    from langgraph.prebuilt.tool_node import ToolInvocationError
    from pydantic import BaseModel, ValidationError

    from app.agent.graph import _handle_tool_error

    class _Args(BaseModel):
        path: str

    try:
        _Args()  # 故意漏掉必填参数
    except ValidationError as exc:
        error = ToolInvocationError("set_wallpaper", exc, {})

    assert _handle_tool_error(error) == error.message
    assert "set_wallpaper" in error.message


async def test_tool_crash_does_not_kill_the_turn(monkeypatch, settings):
    """工具抛异常后，模型仍应拿到结果并给出最终回答。"""
    _patch(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[{"name": "boom", "args": {"text": "x"}, "id": "c1"}]),
            AIMessage(content="工具挂了，我如实告诉你"),
        ],
    )
    graph, _ = graph_module.build_agent_graph(settings, mode="native", tools=[boom])
    result = await graph.ainvoke({"messages": [HumanMessage(content="调用工具")]})

    contents = [str(getattr(message, "content", "")) for message in result["messages"]]
    assert any("TOOL_CRASHED" in text for text in contents)
    assert "工具挂了，我如实告诉你" in contents


# ---------- 系统提示词的身份写法 ----------


def test_system_prompt_does_not_hard_code_a_job_title():
    """身份写成「你是 X 助手」，会和用户的人格请求直接打架。

    实测（scripts/ab_persona.py，每臂 3 次）：只把这一句从「你是旅行规划助手」
    改成「默认擅长旅行规划」，闲聊场景带旅行词的比例从 3/3 降到 0/3，
    字数从 351-435 降到 45-144。而单独去掉「条理清晰」或解绑工具都做不到。

    原因是模型面对「你是 A」+「请当 B」时只能折中：保留 A 的骨架、套上 B 的语气。
    改成「默认擅长」就没有这场冲突了——专长是能力，不是身份。
    """
    prompt = graph_module.SYSTEM_PROMPT
    assert "你是旅行规划助手" not in prompt
    assert "默认擅长旅行规划" in prompt


def test_system_prompt_does_not_demand_lists():
    """「条理清晰」四个字听起来无害，实际是列表的开关：
    模型对它的默认实现就是分点，于是「我好迷茫」被拆成 A/B/C 三类。"""
    assert "条理清晰" not in graph_module.SYSTEM_PROMPT
