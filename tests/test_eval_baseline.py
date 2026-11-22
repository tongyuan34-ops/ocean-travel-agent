"""评测基线的构建、对比与回归判定。"""

from __future__ import annotations

import json

from evals.baseline import (
    build_baseline,
    compare,
    from_report,
    is_regression,
    load,
    render_markdown,
    save,
)


def _report(cases: dict[str, bool], **metrics: float) -> dict:
    summary = {"pass_rate": sum(cases.values()) / len(cases) if cases else 0.0, **metrics}
    return {
        "summary": summary,
        "results": [{"case_id": key, "passed": value} for key, value in cases.items()],
    }


def test_build_baseline_keeps_verdicts_not_answers() -> None:
    baseline = build_baseline(
        {"pass_rate": 0.5, "total": 2, "passed": 1, "total_tokens": 100},
        [
            {
                "case_id": "a",
                "passed": True,
                "latency_ms": 10,
                "judge_score": 5,
                "answer": "很长的正文",
            },
            {"case_id": "b", "passed": False, "latency_ms": 20},
        ],
        label="demo",
        mode="native",
        model="m",
    )
    assert baseline["metrics"]["pass_rate"] == 0.5
    assert baseline["cases"]["a"] == {"passed": True, "judge_score": 5, "latency_ms": 10}
    assert "answer" not in baseline["cases"]["a"]


def test_compare_reports_fixed_regressed_and_scope_changes() -> None:
    baseline = build_baseline(
        {"pass_rate": 0.5},
        [
            {"case_id": "fixed", "passed": False},
            {"case_id": "broken", "passed": True},
            {"case_id": "same", "passed": True},
            {"case_id": "gone", "passed": True},
        ],
    )
    report = _report({"fixed": True, "broken": False, "same": True, "new": True}, pass_rate=0.75)

    result = compare(baseline, report)
    assert result["fixed"] == ["fixed"]
    assert result["regressed"] == ["broken"]
    assert result["added"] == ["new"]
    assert result["removed"] == ["gone"]
    assert result["metrics"]["pass_rate"]["delta"] == 0.25


def test_is_regression_tolerates_small_noise() -> None:
    baseline = build_baseline({"pass_rate": 0.8}, [{"case_id": "a", "passed": True}])
    # 通过率小跌但没有任何「通过→失败」的用例（比如新增用例拉低）
    report = _report({"a": True, "b": False}, pass_rate=0.79)
    result = compare(baseline, report)
    assert not is_regression(result, max_drop=0.02)
    assert is_regression(result, max_drop=0.005)

    # 单条回归必须报，和小幅波动无关
    report = _report({"a": False}, pass_rate=0.8)
    assert is_regression(compare(baseline, report))


def test_render_markdown_has_table_and_sections() -> None:
    baseline = build_baseline(
        {"pass_rate": 0.5, "latency_p95_ms": 1000, "total_tokens": 500},
        [{"case_id": "a", "passed": False}],
        label="base",
    )
    result = compare(
        baseline,
        {
            "summary": {"pass_rate": 0.6, "latency_p95_ms": 900, "total_tokens": 400},
            "results": [{"case_id": "a", "passed": True}],
        },
    )
    text = render_markdown(result)
    assert "与基线对比" in text
    assert "修复（失败→通过）：a" in text
    assert "回归（通过→失败）：无" in text
    assert "+10.00 pp" in text
    assert "更好" in text


def test_save_and_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "baseline.json"
    baseline = build_baseline({"pass_rate": 1.0}, [{"case_id": "a", "passed": True}])
    save(path, baseline)

    loaded = load(path)
    assert loaded is not None
    assert loaded["cases"]["a"]["passed"] is True

    path.write_text(json.dumps({"version": 999, "cases": {}}), encoding="utf-8")
    assert load(path) is None


def test_from_report_accepts_existing_json() -> None:
    report = _report({"a": True}, pass_rate=1.0)
    report["summary"]["label"] = "历史报告"
    report["summary"]["mode"] = "native"
    baseline = from_report(report)
    assert baseline["label"] == "历史报告"
    assert baseline["mode"] == "native"
    assert baseline["cases"]["a"]["passed"] is True
