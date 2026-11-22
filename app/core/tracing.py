"""链路追踪：一个 trace_id 串起「一条日志 → 一次 SSE 流 → 一条追踪记录」。

为什么需要它：
Agent 的 bug 几乎都不是崩溃，而是「这轮为什么选了错工具 / 为什么说查不到」。
没有 trace_id 时，只能靠时间戳在几百行日志里猜哪几行属于同一次对话。

设计取舍：
- **不装 LangSmith / Langfuse 也能用**。配了 Key 才上报；没配时 trace_id
  仍然生成并打进日志与 SSE 事件，本地排查照样有效。
- trace_id 由我们自己生成（16 位 hex），不依赖上游 run id。
  上游的 run id 是树状的、一次对话有几十个，不适合当对外标识。
- 两个后端**可以同时开**，互不干扰：LangSmith 走环境变量（LangChain 自己认），
  Langfuse 走 callback handler（需要显式构造客户端）。
"""

from __future__ import annotations

import logging
import os
import threading
from contextvars import ContextVar, Token
from typing import Any
from uuid import uuid4

from app.core.config import Settings

logger = logging.getLogger(__name__)

# 当前请求的 trace_id。空串表示不在追踪上下文里（如启动期、脚本直调）
current_trace_id: ContextVar[str] = ContextVar("current_trace_id", default="")


def new_trace_id() -> str:
    """生成一个新的 trace_id。

    取 16 位而非完整 uuid：日志里一行一个，短一点更好读；
    碰撞概率（1/2^64）在单机量级下可以忽略。
    """
    return uuid4().hex[:16]


def set_trace(trace_id: str) -> Token[str]:
    """进入追踪上下文，返回可用于还原的 token。"""
    return current_trace_id.set(trace_id)


def reset_trace(token: Token[str]) -> None:
    """退出追踪上下文。必须在 finally 里调用，否则会污染同一任务的后续请求。"""
    current_trace_id.reset(token)


def get_trace_id() -> str:
    return current_trace_id.get()


def configure_tracing(settings: Settings) -> bool:
    """按配置打开 LangSmith 上报，返回是否启用。

    LangChain 通过环境变量识别追踪开关，所以这里做的是「把配置写回环境」。
    用 setdefault 而非直接赋值：显式导出的环境变量优先级更高，便于临时覆盖。
    """
    if not settings.langsmith_tracing:
        return False
    if not settings.langsmith_api_key:
        logger.warning("已开启 LANGSMITH_TRACING 但缺少 LANGSMITH_API_KEY，追踪不会生效")
        return False

    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    if settings.langsmith_endpoint:
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
    # 旧版变量名，部分 langchain 版本仍只认这个
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")

    logger.info(
        "链路追踪已启用 | project=%s endpoint=%s",
        settings.langsmith_project,
        settings.langsmith_endpoint or "https://api.smith.langchain.com",
    )
    return True


def trace_config(trace_id: str, **metadata: Any) -> dict[str, Any]:
    """构造传给 astream_events 的 config。

    run_name 与 metadata 会一并出现在 LangSmith 的 trace 列表里，
    这样从面板上能直接按 session_id / user_id 过滤，不必先翻日志找 trace_id。

    Langfuse 认的是 ``langfuse_session_id`` / ``langfuse_user_id`` /
    ``langfuse_trace_name`` 这几个键（见它的 LangChain handler 源码），
    所以启用时顺手多带一份——两边用同一段 metadata，不必在调用处分支。

    **回调挂在这里，不挂在模型上。** 挂在模型上只能拿到 generation，
    看不到 chain 与 tool 这两层——而排查 Agent 问题时最想看的恰恰是
    「它调了哪个工具、参数是什么、返回了什么」。放在 config 里会随
    astream_events 传给每一个子 run，一次覆盖全部层级。
    """
    payload = {"trace_id": trace_id, **{k: v for k, v in metadata.items() if v}}
    config: dict[str, Any] = {
        "run_name": f"chat-{trace_id}",
        "tags": ["chat"],
        "metadata": payload,
    }
    callbacks = langfuse_callbacks()
    if callbacks:
        if session_id := metadata.get("session_id"):
            payload["langfuse_session_id"] = session_id
        if user_id := metadata.get("user_id"):
            payload["langfuse_user_id"] = user_id
        payload["langfuse_trace_name"] = f"chat-{trace_id}"
        config["callbacks"] = callbacks
    return config


# ---------- Langfuse（可选后端） ----------
#
# 和 LangSmith 的实现方式完全不同，别把两者混着看：
# - LangSmith 由 LangChain 自己读环境变量，我们只要把配置写回 os.environ；
# - Langfuse 的 CallbackHandler **不带凭据参数**，它只认进程里的单例，
#   而单例要靠先 ``Langfuse(...)`` 注册出来。顺序反了就静默不上报。

_langfuse_lock = threading.Lock()
_langfuse_client: Any = None
_langfuse_handler: Any = None
_langfuse_settled = False


def configure_langfuse(settings: Settings) -> bool:
    """按配置初始化 Langfuse，返回是否启用。

    三条约束，每一条都来自踩过的坑：

    - **import 放在函数里**：langfuse 连带 opentelemetry + protobuf，冷启动实测
      多两三秒，而多数用户根本不配追踪。和 ``app/agent/model.py`` 延迟导入
      langchain_openai 是同一个理由。
    - **只尝试一次**：初始化失败（网络不通、key 写错）之后每轮都重试，
      等于每轮多等一次超时。``_langfuse_settled`` 记住结论。
    - **失败绝不上抛**：追踪是附加能力，不能因为它没配好就让对话起不来。
    """
    global _langfuse_client, _langfuse_handler, _langfuse_settled

    with _langfuse_lock:
        if _langfuse_handler is not None:
            return True
        if _langfuse_settled:
            # 启动阶段试过一次但没成功：允许用当前配置再试一次。
            # 桌面版窗口模式下 create_app 可能被提前调用，那时配置还没就绪。
            _langfuse_settled = False
        _langfuse_settled = True

        logger.info(
            "Langfuse 初始化检查 | tracing=%s pk=%s sk=%s host=%s",
            settings.langfuse_tracing,
            "有" if settings.langfuse_public_key else "无",
            "有" if settings.langfuse_secret_key else "无",
            settings.langfuse_host or "",
        )
        if not settings.langfuse_tracing:
            return False
        if not settings.langfuse_public_key or not settings.langfuse_secret_key:
            logger.warning(
                "已开启 LANGFUSE_TRACING 但缺少 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY，追踪不会生效"
            )
            return False

        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
        except ImportError:
            logger.warning(
                "配了 LANGFUSE_* 但没装 langfuse 包（pip install -e '.[tracing]'），追踪不会生效"
            )
            return False

        try:
            _langfuse_client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host or None,
                tracing_enabled=True,
                # 版本号带上去：重打包之后能一眼看出某条 trace 是哪个版本产生的
                release=_release(),
            )
            _langfuse_handler = CallbackHandler()
        except Exception:
            logger.exception("Langfuse 初始化失败，本次运行不再上报")
            _langfuse_client = None
            _langfuse_handler = None
            return False

        logger.info(
            "Langfuse 追踪已启用 | host=%s",
            settings.langfuse_host or "https://cloud.langfuse.com",
        )
        return True


def langfuse_enabled() -> bool:
    return _langfuse_handler is not None


def langfuse_callbacks() -> list[Any]:
    """要挂到模型上的回调；未启用时是空列表。

    LangChain 对空列表不做任何事，所以调用处不必写 if。
    """
    if _langfuse_handler is None:
        # 兜底：启动时没初始化成功（例如窗口模式提前建了应用），这里再试一次
        try:
            from app.core.config import load_settings

            configure_langfuse(load_settings())
        except Exception:  # noqa: BLE001 - 追踪失败不能影响对话
            pass
    return [_langfuse_handler] if _langfuse_handler is not None else []


def flush_tracing() -> None:
    """把队列里还没发出去的事件刷出去。

    桌面版关窗即退，后台批量上报不会自己跑完——不 flush 就等于这几轮白追踪。
    异常一律吞掉：追踪后端挂了也不该让程序退不掉。
    """
    if _langfuse_client is None:
        return
    try:
        _langfuse_client.flush()
    except Exception:
        logger.warning("刷新 Langfuse 事件失败（不影响退出）", exc_info=True)


def shutdown_tracing() -> None:
    """进程退出前收尾：flush 并停掉后台线程。"""
    global _langfuse_client, _langfuse_handler
    if _langfuse_client is None:
        return
    try:
        _langfuse_client.shutdown()
    except Exception:
        logger.warning("关闭 Langfuse 客户端失败（不影响退出）", exc_info=True)
    finally:
        _langfuse_client = None
        _langfuse_handler = None


def _release() -> str:
    """给 trace 打上版本标签；取不到就留空，不值得为它抛异常。"""
    try:
        from app.version import __version__

        return __version__
    except Exception:  # noqa: BLE001
        return ""
