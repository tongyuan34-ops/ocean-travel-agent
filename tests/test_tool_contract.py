"""工具契约测试：``args_schema`` 与真实函数签名必须一致。

这类 bug 很隐蔽，因为两边单独看都「没错」：

.. code-block:: python

    class PathInput(BaseModel):
        path: str

    @tool(args_schema=PathInput)
    async def set_wallpaper(image_path: str) -> str: ...

模型只能看到 ``args_schema``，于是老老实实传 ``path``；LangChain 按 schema
校验通过，再把 ``path`` 原样转发给协程，协程的形参却叫 ``image_path`` ——
``TypeError: set_wallpaper() got an unexpected keyword argument 'path'``。

这个 bug 是真跑评测时才暴露的：静态检查（ruff/mypy）看不出来，
权限、重试、预算的单元测试也都覆盖不到，因为它们测的是别的层。
所以这里按**所有工具**统一校验，新增工具自动纳入，不用记得补测试。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from app.agent.resilience import RetryPolicy, make_resilient
from app.agent.tools import ALL_TOOLS


def _underlying(tool: BaseTool) -> Any:
    """取工具真正被调用的那个可调用对象。"""
    func = getattr(tool, "coroutine", None) or getattr(tool, "func", None)
    assert func is not None, f"{tool.name} 既没有 coroutine 也没有 func"
    return func


def _signature(tool: BaseTool) -> inspect.Signature:
    return inspect.signature(_underlying(tool))


def _schema_fields(tool: BaseTool) -> set[str]:
    schema = tool.args_schema
    if schema is None:
        return set()
    if isinstance(schema, dict):
        return set((schema.get("properties") or {}).keys())
    # args_schema 允许 pydantic v1 的模型，那里没有 model_fields
    fields = getattr(schema, "model_fields", None)
    if fields is None:
        return set(getattr(schema, "__fields__", {}))
    return set(fields)


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[item.name for item in ALL_TOOLS])
def test_schema_fields_are_accepted_by_function(tool: BaseTool) -> None:
    """schema 声明的每个参数，函数签名都得收得下。"""
    params = _signature(tool).parameters
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
        pytest.skip(f"{tool.name} 接受 **kwargs，无法静态校验")

    extra = _schema_fields(tool) - set(params)
    assert not extra, (
        f"{tool.name} 的 args_schema 声明了 {sorted(extra)}，"
        f"但函数形参是 {sorted(params)} —— 模型照着 schema 传参就会 TypeError"
    )


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[item.name for item in ALL_TOOLS])
def test_required_function_params_are_declared(tool: BaseTool) -> None:
    """函数必填参数必须在 schema 里出现，否则模型永远填不上。"""
    fields = _schema_fields(tool)
    if not fields and tool.args_schema is None:
        pytest.skip(f"{tool.name} 没有 args_schema")

    missing = [
        name
        for name, param in _signature(tool).parameters.items()
        if param.default is inspect.Parameter.empty
        and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in fields
    ]
    assert not missing, f"{tool.name} 的必填参数 {missing} 没在 args_schema 里声明"


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=[item.name for item in ALL_TOOLS])
def test_resilient_wrapper_keeps_contract(tool: BaseTool) -> None:
    """重试包装只换实现，不能动名字、描述和参数 schema。"""
    wrapped = make_resilient(tool, RetryPolicy(attempts=1))
    assert wrapped.name == tool.name
    assert wrapped.description == tool.description
    assert _schema_fields(wrapped) == _schema_fields(tool)


def test_wallpaper_takes_path_keyword() -> None:
    """回归锚点：set_wallpaper 曾经把形参写成 image_path，和 schema 对不上。"""
    from app.agent.tools.system import set_wallpaper

    func = getattr(set_wallpaper, "coroutine", None)
    assert func is not None
    params = inspect.signature(func).parameters
    assert "path" in params
    assert "image_path" not in params


# 三个工具都自称「路线」，模型只能靠 description 分辨。
# 只写「我做什么」不够——40 条评测里 `trip-chongqing-2d` 三轮都选了
# `optimize_day_route`，因为「顺路」这个词只出现在它的描述里；
# `plan-then-optimize` 连「先规划三天行程」这一步都漏了。
# 有效的是「什么时候**不要**用我」，并且点名另一个工具。
_ROUTE_TOOL_NAMES = {
    "plan_trip": ("plan_route", "optimize_day_route"),
    "plan_route": ("plan_trip", "optimize_day_route"),
    "optimize_day_route": ("plan_trip",),
}


@pytest.mark.parametrize("tool_name", sorted(_ROUTE_TOOL_NAMES))
def test_route_tools_disambiguate_against_each_other(tool_name: str) -> None:
    from app.agent.tools import ALL_TOOLS

    tool = next(item for item in ALL_TOOLS if item.name == tool_name)
    description = tool.description or ""
    assert "不要" in description, f"{tool_name} 没写「什么时候不要用我」，模型会误选"
    for other in _ROUTE_TOOL_NAMES[tool_name]:
        assert other in description, f"{tool_name} 的描述里没点名 {other}，模型无从比较"


def test_budget_tool_refuses_to_guess_prices() -> None:
    """`estimate_budget` 只做算术。

    早先用例「两个人去厦门四天大概多少钱」期望它被调用，但它要跑起来就得先编出
    机票酒店价——和「不编造价格」的底线正面冲突。现在把这条写进 description，
    并改成反向用例 `budget-without-amounts`。
    """
    from app.agent.tools import ALL_TOOLS

    tool = next(item for item in ALL_TOOLS if item.name == "estimate_budget")
    description = tool.description or ""
    assert "不要调用本工具" in description
    assert "编造" in description or "编出" in description
