"""评测入口：``python -m evals.run``。

两种用法：

    # 离线自测，验证框架本身（CI 里跑这个）
    python -m evals.run --self-test

    # 用真实模型跑用例集，出报告
    python -m evals.run --out evals/report.md --json evals/report.json

自测只证明「评测框架能正确观测工具序列、用量与断言」，
不证明模型表现——那必须用真模型跑，也必然要花钱。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from app.agent.graph import build_agent_graph
from app.agent.model import build_chat_model
from app.agent.resilience import ToolBudget
from app.core.config import Settings, load_settings
from evals.fake_model import ScriptedChatModel, tool_call_message
from evals.runner import (
    DEFAULT_CASES,
    DEFAULT_TIMEOUT_SEC,
    PASS_JUDGE_SCORE,
    CaseResult,
    RunOutcome,
    judge_answer,
    load_cases,
    render_markdown,
    run_graph,
    score_case,
    score_execution,
    score_text,
    summarize,
    tool_trace,
)
from evals.stubs import make_stub_tools


def _script_for(case: dict[str, Any]) -> list[AIMessage]:
    """把用例里的 self_test.script 编译成模型剧本。"""
    script: list[AIMessage] = []
    for step in (case.get("self_test") or {}).get("script") or []:
        if "tool" in step:
            script.append(tool_call_message(str(step["tool"]), dict(step.get("args") or {})))
        else:
            script.append(AIMessage(content=str(step.get("final") or "")))
    if not script:
        script.append(AIMessage(content="（自测用例没有提供剧本）"))
    return script


async def _run_one(
    case: dict[str, Any],
    *,
    settings: Settings,
    graph: Any,
    judge_model: Any,
    timeout: float,
) -> CaseResult:
    case_id = str(case["id"])
    category = str(case.get("category") or "uncategorized")
    started = time.perf_counter()
    budget = ToolBudget(
        per_tool=settings.tool_max_calls_per_tool,
        total=settings.tool_max_calls_total,
    )
    outcome = RunOutcome()
    error = ""
    try:
        outcome = await run_graph(
            graph, str(case["input"]), timeout, budget, persona=case.get("persona")
        )
    except TimeoutError:
        error = f"用例超时（>{timeout:.0f}s）"
    except Exception as exc:  # noqa: BLE001 - 单条用例失败不能中断整轮
        error = f"{type(exc).__name__}: {exc}"

    tools, executed, answer = outcome.tools, outcome.executed, outcome.answer
    usage, final_state, taint = outcome.usage, outcome.final_state, outcome.taint

    latency_ms = int((time.perf_counter() - started) * 1000)
    missing, forbidden_called, missing_mentions = score_case(case, answer, tools)
    forbidden_executed = score_execution(case, executed)
    text_violations = score_text(case, answer)
    # 阴性对照用：正常文档不该触发污点闸。只断言「拦到了」会让检测器
    # 越激进分越高，必须同时有用例反过来约束它别乱拦。
    max_guard = case.get("max_guard_blocks")
    guard_within_limit = max_guard is None or taint.blocked <= int(max_guard)

    judge_score: int | None = None
    judge_reason = ""
    if judge_model is not None and answer:
        # 必须把工具轨迹一起交给评审，否则它会把工具查到的数字判成编造
        judge_score, judge_reason = await judge_answer(
            judge_model, case, answer, tool_trace(final_state)
        )

    passed = (
        not error
        and not missing
        and not forbidden_called
        and not forbidden_executed
        and not missing_mentions
        and not text_violations
        and guard_within_limit
        and (judge_score is None or judge_score >= PASS_JUDGE_SCORE)
    )

    return CaseResult(
        case_id=case_id,
        category=category,
        passed=passed,
        answer=answer,
        tools=tools,
        executed_tools=executed,
        expected_tools=[str(item) for item in case.get("expect_tools") or []],
        forbidden_tools=[str(item) for item in case.get("forbid_tools") or []],
        forbidden_executed_tools=[str(item) for item in case.get("forbid_executed") or []],
        missing_tools=missing,
        forbidden_called=forbidden_called,
        forbidden_executed=forbidden_executed,
        missing_mentions=missing_mentions,
        text_violations=text_violations,
        judge_score=judge_score,
        judge_reason=judge_reason,
        latency_ms=latency_ms,
        model_calls=usage.calls if usage is not None else 0,
        budget_blocked=budget.blocked,
        guard_blocked=taint.blocked,
        taint=taint.to_dict(),
        usage=usage.to_dict(settings.model_name, settings.model_price) if usage else {},
        error=error,
    )


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    cases = load_cases(Path(args.cases))
    if args.self_test:
        # 只跑带剧本的用例：给所有用例编剧本会让自测「全绿」却什么都没验证
        skipped = [str(item["id"]) for item in cases if not item.get("self_test")]
        cases = [item for item in cases if item.get("self_test")]
        if skipped:
            print(
                f"自测模式：跳过 {len(skipped)} 条无剧本用例"
                f"（{'、'.join(skipped[:5])}{' 等' if len(skipped) > 5 else ''}）",
                file=sys.stderr,
            )
    if args.category:
        wanted = {item.strip() for item in args.category.split(",") if item.strip()}
        unknown = wanted - {str(item.get("category") or "uncategorized") for item in cases}
        if unknown:
            print(f"未知分类：{'、'.join(sorted(unknown))}", file=sys.stderr)
            return 2
        cases = [item for item in cases if str(item.get("category") or "") in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有可执行的用例。", file=sys.stderr)
        return 2

    stub_tools = make_stub_tools() if args.self_test else None
    # 自测不调真模型，也就没有评审模型；真跑时评审复用主模型
    judge_model = None if args.self_test or args.no_judge else build_chat_model(settings)
    shared: tuple[Any, str] | None = None
    if not args.self_test:
        shared = build_agent_graph(settings, mode=args.mode, tools=stub_tools)

    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        if args.self_test:
            model = ScriptedChatModel(script=_script_for(case))
            graph, mode = build_agent_graph(settings, mode="native", tools=stub_tools, model=model)
        else:
            assert shared is not None
            graph, mode = shared

        result = await _run_one(
            case,
            settings=settings,
            graph=graph,
            judge_model=judge_model,
            timeout=args.timeout,
        )
        results.append(result)
        mark = "PASS" if result.passed else "FAIL"
        notes = ""
        if result.budget_blocked:
            notes += f"  budget_blocked={result.budget_blocked}"
        if result.guard_blocked:
            notes += f"  guard_blocked={result.guard_blocked}"
        print(
            f"[{index:>2}/{len(cases)}] {mark} {result.case_id}"
            f"  tools={'->'.join(result.tools) or '-'}"
            f"  {result.latency_ms}ms" + notes,
            file=sys.stderr,
        )

    summary = summarize(results)
    label = args.label or ("离线自测（框架验证）" if args.self_test else f"{mode} 模式")
    summary["label"] = label
    summary["mode"] = mode
    summary["self_test"] = bool(args.self_test)

    report = render_markdown(summary, results, label=label)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "results": [item.to_dict() for item in results]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print(report)
    if args.fail_under is not None:
        rate = summary["pass_rate"] or 0.0
        if rate < args.fail_under:
            print(
                f"通过率 {rate:.1%} 低于阈值 {args.fail_under:.1%}",
                file=sys.stderr,
            )
            return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evals.run", description="Agent 评测")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="用例文件路径")
    parser.add_argument("--out", default="", help="Markdown 报告输出路径")
    parser.add_argument("--json", default="", help="JSON 结果输出路径")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    parser.add_argument(
        "--category",
        default="",
        help="只跑指定分类，逗号分隔（如 tool_selection,honesty）",
    )
    parser.add_argument(
        "--mode", default="", choices=["", "native", "structured"], help="强制 agent 模式"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SEC, help="单条用例超时秒数"
    )
    parser.add_argument("--self-test", action="store_true", help="用脚本化模型离线自测框架")
    parser.add_argument("--no-judge", action="store_true", help="跳过 LLM-as-judge 打分")
    parser.add_argument("--label", default="", help="报告标题")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="通过率低于该值时以非零码退出（0-1），默认不检查",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
