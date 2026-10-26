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
