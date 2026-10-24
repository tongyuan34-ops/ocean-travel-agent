"""模型接入层。

落实 SPEC 三处要求：
- 4.3 模型层可切换 provider，业务代码不直连厂商 SDK
- 4.4 支持原生 tool calling 就用 bind_tools，不支持则回退结构化输出
- 4.5 本地模型必须绕过系统代理，并有超时与长度上限
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import Settings

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _host_of(base_url: str) -> str:
    try:
        return urlparse(base_url).hostname or ""
    except ValueError:
        return ""


def _is_local(base_url: str) -> bool:
    return _host_of(base_url) in _LOCAL_HOSTS


def _ensure_no_proxy() -> None:
    """本地模型请求绝不能走系统代理，否则会被代理拦下导致超时。"""
    current = os.environ.get("NO_PROXY", "")
    parts = {item.strip() for item in current.split(",") if item.strip()}
    parts.update(_LOCAL_HOSTS)
    os.environ["NO_PROXY"] = ",".join(sorted(parts))


def _needs_thinking_disabled(host: str) -> bool:
    """这些服务商的模型**默认开启思考模式**，必须显式关掉。

    思考 token 是要算进 max_tokens 的：实测方舟上的
    deepseek-v4-1-flash-economy，一次 max_tokens=8 的调用会把 8 个 token
    全烧在 reasoning 上，content 直接为空、finish_reason=length——
    agent 就会拿到空回答并触发 MODEL_EMPTY_RESPONSE。

    只在明确支持该参数的服务商上注入：本地 LM Studio / 其它 provider
    收到不认识的 `thinking` 字段会直接 400。
    """
    return host == "api.deepseek.com" or host.endswith("volces.com")


def build_chat_model(settings: Settings) -> ChatOpenAI:
    """按配置构造聊天模型。"""
    if not settings.model_name or not settings.model_base_url:
        raise ValueError("MODEL_NAME / MODEL_BASE_URL 未配置，请在 .env 中填写")

    local = _is_local(settings.model_base_url)
    if local:
        _ensure_no_proxy()
    elif not settings.model_api_key:
        raise ValueError("远程模型服务必须配置 MODEL_API_KEY")

    # 用 dict 收集参数再展开：ChatOpenAI 的多数字段是 pydantic alias，
    # 直接写关键字参数会让 mypy 认为参数名不存在（其实是有的）
    kwargs: dict[str, Any] = {
        "model": settings.model_name,
        "api_key": SecretStr(settings.model_api_key or "not-needed"),
        "base_url": settings.model_base_url,
        "use_responses_api": False,
        "timeout": 300,
        "max_tokens": 4096,
        "max_retries": 0,
    }
    if _needs_thinking_disabled(_host_of(settings.model_base_url)):
        # 开发期先关闭思考模式，只练普通对话与工具调用
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    return ChatOpenAI(**kwargs)


def _ping(value: int) -> str:
    """连通性测试工具：接收一个整数并返回 pong。"""
    return f"pong:{value}"


_PING_TOOL = StructuredTool.from_function(
    func=_ping,
    name="ping",
    description="连通性测试工具，接收一个整数 value，返回 pong:<value>",
)


def detect_tool_calling(model: ChatOpenAI, settings: Settings) -> str:
    """探测模型能力，返回 'native' 或 'structured'。

    auto（默认）会做一次真实探测：给模型一个最小工具，看它是否返回 tool_calls。
    只消耗一次极小调用；探测失败则保守回退，绝不假装支持。
    """
    mode = settings.model_tool_calling
    if mode in {"native", "structured"}:
        logger.info("tool calling 模式由配置指定：%s", mode)
        return mode

    try:
        probe = model.bind_tools([_PING_TOOL])
        reply = probe.invoke("请调用 ping 工具，把 value 设为 1。")
        if getattr(reply, "tool_calls", None):
            logger.info("探测结果：模型支持原生 tool calling")
            return "native"
        logger.warning("模型未返回 tool_calls，回退到 structured 模式")
    except Exception as exc:  # noqa: BLE001 - 探测失败不应阻断启动
        logger.warning("tool calling 探测失败（%s），回退到 structured 模式", exc)
    return "structured"
