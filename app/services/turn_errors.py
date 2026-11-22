"""上游异常 → 契约错误码。

单独成模块的理由：这里全是「用户该去哪儿修」的文案，和聊天主流程没有关系，
但每一条都容易写错方向——把欠费说成「检查 Key」，用户会一直点重试。
"""

from __future__ import annotations

from app.core import secrets

# 上游账单类错误的关键词。命中了就说明「重试」永远没用——
# 火山方舟欠费返回 403 AccountOverdueError，OpenAI 是 429 insufficient_quota，
# 有的网关只给一句 "insufficient balance"。这些都是账户状态问题，
# 归到 INTERNAL_ERROR 会给出「请稍后重试」，等于让用户白试。
QUOTA_HINTS = (
    "overdue",
    "arrears",
    "insufficient_quota",
    "insufficient balance",
    "exceeded your current quota",
    "billing",
    "欠费",
    "余额不足",
    "额度",
)
# 上游鉴权失败：Key 写错、被吊销、没有该模型的权限
AUTH_HINTS = ("invalid api key", "incorrect api key", "unauthorized", "authentication")

# key 来源 → 用户该去哪儿改。说错地方等于让他去一个不存在的页面
KEY_PLACES = {
    secrets.CONFIG: "config.json（在程序目录里）",
    secrets.ENV: ".env 或环境变量",
    secrets.BUNDLED: "内置配置",
}


def quota_message() -> str:
    """额度用尽时该说什么，取决于这个 key 是谁的。

    内置 key 的用户**没有服务商控制台可去**——那不是他的账号。他能做的
    只有一件事：填自己的 key。这条分支不写，桌面版用户会一直点重试。
    """
    if secrets.source("MODEL_API_KEY") == secrets.BUNDLED:
        return (
            "内置额度已用完。在程序目录的 config.json 里把 MODEL_API_KEY "
            "换成你自己的，重启就能继续用"
        )
    return "请到服务商控制台充值或补缴欠款后再试（重试不会恢复）"


def auth_message() -> str:
    place = KEY_PLACES.get(secrets.source("MODEL_API_KEY"), "配置")
    return f"请检查 {place} 里的 MODEL_API_KEY 是否填错或被吊销"


def map_failure(exc: Exception) -> dict[str, str]:
    """把异常映射成契约错误码。

    顺序有讲究：**先认账单，再认鉴权，最后才看状态码**。
    403 既可能是欠费也可能是 Key 没权限，只看状态码会把「去充值」说成
    「检查 Key」；而这两件事都不是重试能解决的，说错方向用户会一直点重试。
    """
    name = type(exc).__name__
    text = str(exc)
    lowered = text.lower()
    status = getattr(exc, "status_code", None)

    if any(hint in lowered for hint in QUOTA_HINTS):
        return {"code": "MODEL_QUOTA_EXCEEDED", "message": quota_message()}

    if status in (401, 403) or any(hint in lowered for hint in AUTH_HINTS):
        return {"code": "MODEL_AUTH_FAILED", "message": auth_message()}

    # 「timed out」和「timeout」两种写法都要认：openai SDK 的异常名是
    # APITimeoutError，但有的网关只在正文里写 "Request timed out"
    if "Timeout" in name or "timeout" in lowered or "timed out" in lowered:
        return {"code": "MODEL_TIMEOUT", "message": f"模型响应超时：{text}"}
    if "Connection" in name or "Connect" in name:
        return {"code": "MODEL_UNAVAILABLE", "message": f"无法连接模型服务：{text}"}
    return {"code": "INTERNAL_ERROR", "message": f"处理失败：{text}"}
