"""agent 图：native 与 structured 两条路径都要能完成多步工具调用（SPEC 4.4）。

模型换成可脚本化的假模型、工具换成桩，
校验的是"两条路径都能跑通"，不是模型能力或外部 API。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
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


class StreamingChatModel(ScriptedChatModel):
    """逐块吐字的模型：用来验证 native 节点真的走流式，而不是一次性 invoke。"""

    pieces: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(content="".join(self.pieces))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for piece in self.pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for piece in self.pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


async def test_native_mode_streams_tokens_through_events(monkeypatch, settings):
    """native 节点必须逐块产出 ``on_chat_model_stream``。

    回归：节点曾是同步 ``bound.invoke``，LangGraph 把它丢进线程池执行，
    回调上下文在那个线程里丢失，``astream_events`` 收不到任何 stream 事件。
    症状是回答在服务端生成完毕、也落了库，前端却一个字都收不到，
    最后只能报「模型未返回内容」——打包版稳定复现，源码环境偶发。
    """
    model = StreamingChatModel(pieces=["三", "亚", "攻略"])
    monkeypatch.setattr(graph_module, "build_chat_model", lambda _s: model)
    graph, mode = graph_module.build_agent_graph(settings, mode="native", tools=[echo])
    assert mode == "native"

    pieces: list[str] = []
    async for event in graph.astream_events(
        {"messages": [HumanMessage(content="你好")]}, version="v2"
    ):
        if event["event"] == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            pieces.append(content if isinstance(content, str) else str(content))

    # 末尾可能带一个空 chunk（有的 provider 用它标记结束），只校验有内容的块
    assert [piece for piece in pieces if piece] == ["三", "亚", "攻略"]


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


def test_permission_denied_is_not_a_crash():
    """用户点「拒绝」是预期结果，不是工具崩溃。

    早先它落进 TOOL_CRASHED 分支，后果有两个：每次拒绝都打一条带 traceback 的
    ERROR 日志（真正的崩溃会被淹掉）；评测的 `tool_outcomes` 把它记成
    「工具真的执行了」，于是「禁用工具真的执行」这个指标失真。
    """
    from app.agent.graph import _handle_tool_error
    from app.agent.permissions import PermissionDenied

    result = _handle_tool_error(PermissionDenied("用户拒绝了该操作"))
    assert result.startswith("TOOL_DENIED")
    assert "用户拒绝了该操作" in result
    assert "TOOL_CRASHED" not in result
    assert "PermissionDenied" not in result


def test_denied_result_counts_as_blocked_in_evals():
    """评测口径：被拒绝 = 被拦下，不能算「真的执行了」。"""
    from evals.runner import tool_outcomes
    from langchain_core.messages import ToolMessage

    from app.agent.resilience import is_blocked_result

    denied = ToolMessage(
        content="TOOL_DENIED：用户拒绝了该操作", tool_call_id="c1", name="write_file"
    )
    assert is_blocked_result(denied.content)

    state = {
        "messages": [
            AIMessage(content="", tool_calls=[{"name": "write_file", "args": {}, "id": "c1"}]),
            denied,
        ]
    }
    executed, blocked = tool_outcomes(state)
    assert executed == []
    assert blocked == ["write_file"]


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


async def test_tracing_callbacks_propagate_from_config(monkeypatch, settings):
    """回调由调用方通过 config 传入，必须能覆盖循环里的每一次模型调用。

    这一条是给「配了 key 却一条 trace 都没有」兜底的：structured 模式在节点里
    反复调模型，只挂在 graph.ainvoke 外层会不会漏？答案是不会——config 会随
    运行树传进节点，每次 ``ainvoke`` 都继承。
    """
    from langchain_core.callbacks import BaseCallbackHandler

    started: list[str] = []

    class Spy(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            started.append("start")

    _patch(monkeypatch, [AIMessage(content='{"final": "好了"}')])

    graph, _ = graph_module.build_agent_graph(settings, mode="structured", tools=[echo])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="你好")]}, config={"callbacks": [Spy()]}
    )

    assert "好了" in [getattr(message, "content", "") for message in result["messages"]]
    assert started == ["start"]


async def test_config_callbacks_do_not_swallow_stream_events(monkeypatch, settings):
    """回归：回调放在 config 里，token 事件一个都不能少。

    曾经把 Langfuse 回调用 ``with_config`` 绑在模型上，模型级事件整组消失
    （连普通 Spy 回调也一样）。桌面版默认开 Langfuse，所以桌面版稳定复现：
    回答生成了、也落库了，前端却 0 个 token，只能显示「模型未返回内容」。
    """
    from langchain_core.callbacks import BaseCallbackHandler

    class Spy(BaseCallbackHandler):
        def on_llm_new_token(self, *args, **kwargs):
            return None

    model = StreamingChatModel(pieces=["三", "亚", "攻略"])
    monkeypatch.setattr(graph_module, "build_chat_model", lambda _s: model)
    graph, _ = graph_module.build_agent_graph(settings, mode="native", tools=[echo])

    pieces: list[str] = []
    async for event in graph.astream_events(
        {"messages": [HumanMessage(content="你好")]},
        version="v2",
        config={"callbacks": [Spy()]},
    ):
        if event["event"] == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            pieces.append(content if isinstance(content, str) else str(content))

    assert [piece for piece in pieces if piece] == ["三", "亚", "攻略"]


async def test_graph_runs_when_tracing_is_disabled(monkeypatch, settings):
    """没配追踪时构图与执行不能因此有任何变化。"""
    _patch(monkeypatch, [AIMessage(content='{"final": "照常"}')])

    graph, _ = graph_module.build_agent_graph(settings, mode="structured", tools=[echo])
    result = await graph.ainvoke({"messages": [HumanMessage(content="你好")]})

    assert "照常" in [getattr(message, "content", "") for message in result["messages"]]
