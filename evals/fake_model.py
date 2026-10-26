"""评测专用：脚本化聊天模型。

自测（``--self-test``）不能依赖真实 API：既没法在 CI 里跑，也没法断言。
这里用按剧本返回的模型替身，让评测框架本身可以被确定性地验证。

它实现的是**真实的** ``BaseChatModel`` 接口，所以走的是真正的
LangGraph 循环、真正的 ToolNode、真正的重试包装——验证的是框架，
不是模型。真正打分仍然要用真模型跑一遍（``python -m evals.run``）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

# 每次模型调用的假用量，用来验证 usage 采集与成本计算的链路
DEFAULT_INPUT_TOKENS = 320
DEFAULT_OUTPUT_TOKENS = 48


class ScriptedChatModel(BaseChatModel):
    """按顺序返回预置消息的模型替身。

    超出剧本长度后重复最后一条，这样剧本写短了也不会直接崩，
    只会表现为「多调了一次模型」，由步数断言去发现。
    """

    script: list[AIMessage] = Field(default_factory=list)
    tools: list[Any] = Field(default_factory=list)
    input_tokens: int = DEFAULT_INPUT_TOKENS
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    # 用列表做计数器：model_copy 是浅拷贝，列表引用共享，
    # 这样 bind_tools 产生的副本和原对象能共用同一个计数
    calls: list[int] = Field(default_factory=lambda: [0])

    @property
    def _llm_type(self) -> str:
        return "scripted-eval"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        # 返回副本而不是 self：LangGraph 会持有绑定后的实例
        return self.model_copy(update={"tools": list(tools)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        index = min(self.calls[0], len(self.script) - 1)
        self.calls[0] += 1
        template = self.script[index] if self.script else AIMessage(content="")

        message = AIMessage(
            content=template.content,
            tool_calls=list(getattr(template, "tool_calls", None) or []),
            usage_metadata={
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_call_message(name: str, args: dict[str, Any], call_id: str = "") -> AIMessage:
    """构造一条「要求调用工具」的消息。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": call_id or f"call_{name}",
                "type": "tool_call",
            }
        ],
    )


__all__ = ["ScriptedChatModel", "tool_call_message"]
