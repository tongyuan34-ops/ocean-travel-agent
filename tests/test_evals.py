"""评测框架自身的逻辑。

这些用例保证「评测结果可信」：如果打分逻辑本身错了，
跑出来的通过率再好看也没有意义。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from evals.run import main
from evals.runner import (
    CaseResult,
    judge_answer,
    load_cases,
    parse_judge_output,
    render_markdown,
    score_case,
    summarize,
    tool_trace,
)


def _case(**kwargs) -> dict:
    base = {"id": "c1", "input": "问题", "category": "tool_selection"}
    base.update(kwargs)
    return base


# ---------- 用例加载 ----------


def test_load_default_cases_is_valid():
    cases = load_cases()
    assert len(cases) >= 30
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))


def test_load_rejects_duplicate_ids(tmp_path: Path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        "cases:\n  - id: dup\n    input: a\n  - id: dup\n    input: b\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复"):
        load_cases(path)


def test_load_rejects_missing_input(tmp_path: Path):
    path = tmp_path / "cases.yaml"
    path.write_text("cases:\n  - id: no-input\n", encoding="utf-8")
    with pytest.raises(ValueError, match="input"):
        load_cases(path)


def test_load_rejects_wrong_shape(tmp_path: Path):
    path = tmp_path / "cases.yaml"
    path.write_text("items: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cases"):
        load_cases(path)


# ---------- 断言打分 ----------


def test_expected_tool_hit():
    missing, forbidden, mentions = score_case(
        _case(expect_tools=["get_weather"], must_mention=["北京"]),
        answer="北京明天多云",
        tools=["get_weather"],
    )
    assert missing == []
    assert forbidden == []
    assert mentions == []


def test_expected_tool_missing():
    missing, _, _ = score_case(_case(expect_tools=["get_weather"]), answer="多云", tools=[])
    assert missing == ["get_weather"]


def test_forbidden_tool_detected():
    """该用专用工具却去搜网页，是最典型的错误，必须能被抓住。"""
    _, forbidden, _ = score_case(
        _case(expect_tools=["plan_route"], forbid_tools=["search_web"]),
        answer="全程 12 公里",
        tools=["search_web"],
    )
    assert forbidden == ["search_web"]


def test_ordered_requires_subsequence():
    case = _case(expect_tools=["list_dir", "read_file"], ordered=True)
    missing, _, _ = score_case(case, answer="ok", tools=["list_dir", "read_file"])
    assert missing == []

    # 顺序反了必须判失败
    missing, _, _ = score_case(case, answer="ok", tools=["read_file", "list_dir"])
    assert missing == ["read_file"]


def test_ordered_allows_extra_tools_between():
    case = _case(expect_tools=["get_weather", "plan_route"], ordered=True)
    missing, _, _ = score_case(case, answer="ok", tools=["get_weather", "search_poi", "plan_route"])
    assert missing == []


def test_unordered_ignores_sequence():
    case = _case(expect_tools=["get_weather", "plan_route"])
    missing, _, _ = score_case(case, answer="ok", tools=["plan_route", "get_weather"])
    assert missing == []


def test_missing_mention_detected():
    _, _, mentions = score_case(_case(must_mention=["确认"]), answer="已经帮你写好了", tools=[])
    assert mentions == ["确认"]


# ---------- 评审输出解析 ----------


def test_parse_judge_output_plain_json():
    score, reason = parse_judge_output('{"score": 4, "reason": "基本准确"}')
    assert score == 4
    assert reason == "基本准确"


def test_parse_judge_output_with_surrounding_text():
    score, _ = parse_judge_output('评审结果如下：{"score": 2, "reason": "编造温度"} 完毕')
    assert score == 2


def test_parse_judge_output_clamps_range():
    assert parse_judge_output('{"score": 9}')[0] == 5
    assert parse_judge_output('{"score": 0}')[0] == 1


def test_parse_judge_output_failure_returns_none():
    """解析失败必须是 None 而不是 0：0 会拉低平均分并假装「评过了」。"""
    score, reason = parse_judge_output("这个回答还不错")
    assert score is None
    assert "JSON" in reason


# ---------- 工具轨迹（评审的输入） ----------


def _state(*messages):
    return {"messages": list(messages)}


def test_tool_trace_records_call_and_result():
    """轨迹要同时包含「调了什么」和「拿回了什么」，缺一半评审就没法判断。"""
    from langchain_core.messages import AIMessage, ToolMessage

    state = _state(
        AIMessage(
            content="", tool_calls=[{"name": "get_weather", "args": {"city": "北京"}, "id": "1"}]
        ),
        ToolMessage(content="北京 25 度 晴", tool_call_id="1"),
    )
    trace = tool_trace(state)
    assert "get_weather" in trace
    assert "北京" in trace
    assert "25 度" in trace


def test_tool_trace_truncates_long_results():
    """read_file 可能返回几万字，整条塞进评审 prompt 会把 rubric 挤掉。"""
    from langchain_core.messages import AIMessage, ToolMessage

    state = _state(
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "1"}]),
        ToolMessage(content="x" * 5000, tool_call_id="1"),
    )
    trace = tool_trace(state, per_result=100)
    assert len(trace) < 500
    assert "已截断" in trace


def test_tool_trace_empty_state():
    assert "无" in tool_trace(None)
    assert "无" in tool_trace(_state())


async def test_run_graph_feeds_trace_from_real_state(settings):
    """端到端验证轨迹的数据来源：图状态里确实带着 ToolMessage。

    上面几条测的是 ``tool_trace`` 的纯函数逻辑，喂的是手工造的消息。
    这条走真实图，防的是「final_state 里其实没有工具消息」这类接线错误——
    那样评审照样瞎，而单测全绿。
    """
    from evals.fake_model import ScriptedChatModel, tool_call_message
    from evals.runner import run_graph
    from evals.stubs import make_stub_tools
    from langchain_core.messages import AIMessage

    from app.agent.graph import build_agent_graph

    model = ScriptedChatModel(
        script=[
            tool_call_message("get_weather", {"city": "北京"}),
            AIMessage(content="北京明天 18~25℃"),
        ]
    )
    graph, _ = build_agent_graph(settings, mode="native", tools=make_stub_tools(), model=model)
    _, answer, _, state = await run_graph(graph, "北京明天天气", timeout=30)

    trace = tool_trace(state)
    assert "get_weather" in trace
    assert "18~25℃" in trace  # 工具返回的内容进了轨迹
    assert "18~25℃" in answer


async def test_judge_prompt_includes_trace():
    """评审 prompt 必须带轨迹，否则它会把工具查到的数字判成编造。"""

    class _Echo:
        prompt = ""

        async def ainvoke(self, messages):
            from langchain_core.messages import AIMessage

            self.prompt = messages[0][1]
            return AIMessage(content='{"score": 5, "reason": "ok"}')

    model = _Echo()
    score, _ = await judge_answer(
        model,
        _case(rubric="回答应准确"),
        "北京今天 25 度",
        "→ 调用 get_weather({})\n← 返回：北京 25 度 晴",
    )
    assert score == 5
    assert "北京 25 度 晴" in model.prompt
    assert "已核实" in model.prompt


# ---------- 汇总 ----------


def _result(**kwargs) -> CaseResult:
    base = {"case_id": "x", "category": "tool_selection", "passed": True}
    base.update(kwargs)
    return CaseResult(**base)


def test_summarize_empty_returns_none_denominators():
    summary = summarize([])
    assert summary["pass_rate"] is None
    assert summary["tool_recall"] is None
    assert summary["total_cost"] is None


def test_summarize_tool_recall_ignores_cases_without_expectation():
    """无期望工具的用例若计入分母，会把召回率稀释成好看的数字。"""
    results = [
        _result(case_id="a", expected_tools=["get_weather"], tools=["get_weather"]),
        _result(case_id="b", expected_tools=[], tools=[]),
    ]
    summary = summarize(results)
    assert summary["tool_recall"] == 1.0


def test_summarize_counts_forbidden_violations():
    results = [
        _result(case_id="a", forbidden_called=["search_web"], passed=False),
        _result(case_id="b"),
    ]
    assert summarize(results)["forbidden_violations"] == 1


def test_summarize_aggregates_cost_and_tokens():
    results = [
        _result(case_id="a", usage={"total_tokens": 100, "cost": 0.001}),
        _result(case_id="b", usage={"total_tokens": 50, "cost": None}),
    ]
    summary = summarize(results)
    assert summary["total_tokens"] == 150
    # 有一条成本未知，只累加已知的，不能把 None 当 0 混进去
    assert summary["total_cost"] == 0.001


def test_render_markdown_lists_failures():
    results = [
        _result(case_id="bad", passed=False, forbidden_called=["search_web"]),
    ]
    report = render_markdown(summarize(results), results, label="测试")
    assert "`bad`" in report
    assert "误调" in report


# ---------- 端到端：离线自测 ----------


def test_self_test_runs_green(capsys):
    """自测必须全绿：它验证的是框架，框架坏了就没资格谈模型表现。"""
    code = main(["--self-test"])
    assert code == 0
    out = capsys.readouterr().out
    assert "用例数" in out
    assert "100.0%" in out
