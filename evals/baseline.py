"""评测基线：把一次真模型运行固化成可比较的快照。

评测报告解决「这次怎么样」，基线解决「比上次好了还是坏了」。没有后者，
每次改 prompt 都只能靠记忆判断，而记忆在 41 条用例、四五个指标面前不可靠。

文件格式刻意保持扁平：

```json
{
  "version": 1,
  "created_at": "2026-11-21T20:00:00+08:00",
  "label": "...", "mode": "native", "model": "...",
  "metrics": {"pass_rate": 0.8049, "latency_p95_ms": 32461, ...},
  "cases": {"weather-beijing": {"passed": true, "latency_ms": 5226, ...}}
}
```

只存判定结果和延迟，不存回答正文——正文动辄几十 KB，进 git 后每次 diff
都很难读；要看具体回答去 ``evals/report.md``。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

BASELINE_VERSION = 1
DEFAULT_BASELINE = Path("evals/baseline.json")

# 参与对比的汇总指标。key 与 runner.summarize() 保持一致
_METRIC_KEYS = (
    "total",
    "passed",
    "pass_rate",
    "tool_recall",
    "avg_judge",
    "latency_p50_ms",
    "latency_p95_ms",
    "total_tokens",
    "total_cost",
)

# 越小越好的指标，渲染变化时要带符号说明
_LOWER_IS_BETTER = frozenset({"latency_p50_ms", "latency_p95_ms", "total_tokens", "total_cost"})
_PERCENT = frozenset({"pass_rate", "tool_recall"})


def build_baseline(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    label: str = "",
    mode: str = "",
    model: str = "",
) -> dict[str, Any]:
    return {
        "version": BASELINE_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": label,
        "mode": mode,
        "model": model,
        "metrics": {key: summary.get(key) for key in _METRIC_KEYS},
        "cases": {
            str(item.get("case_id")): {
                "passed": bool(item.get("passed")),
                "judge_score": item.get("judge_score"),
                "latency_ms": item.get("latency_ms"),
            }
            for item in results
            if item.get("case_id")
        },
    }


def from_report(report: dict[str, Any], *, label: str = "") -> dict[str, Any]:
    """把 ``evals/report.json`` 转成基线。"""
    summary = report.get("summary") or {}
    return build_baseline(
        summary,
        list(report.get("results") or []),
        label=label or str(summary.get("label") or ""),
        mode=str(summary.get("mode") or ""),
        model=str(summary.get("model") or ""),
    )


def load(path: Path = DEFAULT_BASELINE) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cases"), dict):
        return None
    if int(data.get("version") or 0) != BASELINE_VERSION:
        return None
    return data


def save(path: Path, baseline: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def compare(baseline: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """逐条对比。返回的结构同时供 Markdown 渲染与退出码判断使用。"""
    base_cases = baseline.get("cases") or {}
    current_cases = {
        str(item.get("case_id")): bool(item.get("passed"))
        for item in report.get("results") or []
        if item.get("case_id")
    }
    base_passed = {key for key, value in base_cases.items() if value.get("passed")}
    current_passed = {key for key, passed in current_cases.items() if passed}
    removed = set(base_cases) - set(current_cases)

    fixed = sorted((set(base_cases) - base_passed) & current_passed)
    # 基线里有、这次没跑的用例不算回归：那是评测范围变了，不是质量降了
    regressed = sorted((base_passed - current_passed) - removed)
    added = sorted(set(current_cases) - set(base_cases))

    base_metrics = baseline.get("metrics") or {}
    current_metrics = report.get("summary") or {}
    metric_rows = {}
    for key in _METRIC_KEYS:
        before = base_metrics.get(key)
        after = current_metrics.get(key)
        delta = None
        if isinstance(before, int | float) and isinstance(after, int | float):
            delta = after - before
        metric_rows[key] = {"baseline": before, "current": after, "delta": delta}

    return {
        "created_at": str(baseline.get("created_at") or ""),
        "label": str(baseline.get("label") or ""),
        "metrics": metric_rows,
        "fixed": fixed,
        "regressed": regressed,
        "added": added,
        "removed": sorted(removed),
    }


def is_regression(comparison: dict[str, Any], max_drop: float = 0.02) -> bool:
    """有「通过 → 失败」的用例，或通过率跌幅超过阈值，都算回归。

    阈值默认 2 个百分点：模型本身有随机性，1 条用例的抖动（1/41 ≈ 2.4%）
    不该让 nightly 天天红；真有系统性问题时跌幅会明显大于这个数。
    """
    if comparison.get("regressed"):
        return True
    row = (comparison.get("metrics") or {}).get("pass_rate") or {}
    before, after = row.get("baseline"), row.get("current")
    if isinstance(before, int | float) and isinstance(after, int | float):
        return after < before - max_drop
    return False


def render_markdown(comparison: dict[str, Any]) -> str:
    lines = [
        "## 与基线对比",
        "",
        f"基线时间：{comparison.get('created_at') or '未知'}"
        + (f"（{comparison['label']}）" if comparison.get("label") else ""),
        "",
        "| 指标 | 基线 | 本次 | 变化 |",
        "|---|---|---|---|",
    ]
    for key, row in (comparison.get("metrics") or {}).items():
        lines.append(
            f"| {_label(key)} | {_fmt(key, row.get('baseline'))} | "
            f"{_fmt(key, row.get('current'))} | {_delta(key, row.get('delta'))} |"
        )

    lines.append("")
    for title, key in (("修复（失败→通过）", "fixed"), ("回归（通过→失败）", "regressed")):
        items = comparison.get(key) or []
        lines.append(f"- {title}：{'、'.join(items) if items else '无'}")
    if comparison.get("added"):
        lines.append(f"- 新增用例：{'、'.join(comparison['added'])}")
    if comparison.get("removed"):
        lines.append(f"- 基线有、本次未跑：{'、'.join(comparison['removed'])}")
    return "\n".join(lines) + "\n"


def _label(key: str) -> str:
    return {
        "total": "用例数",
        "passed": "通过数",
        "pass_rate": "通过率",
        "tool_recall": "工具召回",
        "avg_judge": "平均 judge 分",
        "latency_p50_ms": "P50 延迟",
        "latency_p95_ms": "P95 延迟",
        "total_tokens": "总 token",
        "total_cost": "总成本",
    }.get(key, key)


def _fmt(key: str, value: Any) -> str:
    if value is None:
        return "-"
    if key in _PERCENT and isinstance(value, int | float):
        return f"{value:.1%}"
    if key in {"avg_judge"} and isinstance(value, int | float):
        return f"{value:.2f}"
    if key in {"latency_p50_ms", "latency_p95_ms"} and isinstance(value, int | float):
        return f"{value:.0f} ms"
    if key in {"total_cost"} and isinstance(value, int | float):
        return f"{value:.4f} 元"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _delta(key: str, delta: Any) -> str:
    if not isinstance(delta, int | float):
        return "-"
    if key in _PERCENT:
        text = f"{delta * 100:+.2f} pp"
    elif key in {"latency_p50_ms", "latency_p95_ms"}:
        text = f"{delta:+.0f} ms"
    elif key == "total_tokens":
        text = f"{delta:+.0f}"
    elif key == "total_cost":
        text = f"{delta:+.4f} 元"
    elif key == "avg_judge":
        text = f"{delta:+.2f}"
    else:
        text = f"{delta:+g}"
    if delta == 0:
        return "持平"
    better = (delta < 0) if key in _LOWER_IS_BETTER else (delta > 0)
    return f"{text}（{'更好' if better else '更差'}）"


__all__ = [
    "DEFAULT_BASELINE",
    "build_baseline",
    "compare",
    "from_report",
    "is_regression",
    "load",
    "render_markdown",
    "save",
]
