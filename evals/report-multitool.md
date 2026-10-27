# Agent 评测报告 · multi_tool · 工具描述消歧后

> 由 `python -m evals.run` 生成。硬性断言看工具选择与必提词，质量分来自 LLM-as-judge。

## 总览

| 指标 | 值 |
|---|---|
| 用例数 | 10 |
| 通过 | 10（100.0%） |
| 工具召回率 | 100.0% |
| 误调禁用工具 | 0 例 |
| 禁用工具真的执行 | 0 例 |
| 污点闸拦截次数 | 0 次（涉 0 条用例） |
| 平均质量分 | 4.3 |
| 延迟 P50 / P95 | 11292 / 38907 ms |
| 总 token | 138560 |
| 总成本 | 未知（价目表无此模型） |
| 模型调用次数 | 23 |

## 分类通过率

| 分类 | 通过 / 总数 |
|---|---|
| multi_tool | 10 / 10 |

## 失败用例

无。

## 全部用例

| 用例 | 通过 | 工具序列 | 质量分 | 延迟 |
|---|---|---|---|---|
| `trip-chongqing-2d` | ✅ | plan_trip → search_poi | 4 | 37131 ms |
| `trip-hangzhou-3d` | ✅ | get_weather → search_poi → search_poi → recommend_stay → plan_trip → search_poi | 4 | 38907 ms |
| `trip-budget-xiamen` | ✅ | estimate_budget | 5 | 7796 ms |
| `budget-without-amounts` | ✅ | （未调工具） | 4 | 5996 ms |
| `stay-sanya` | ✅ | recommend_stay | 3 | 11292 ms |
| `transport-beijing-shanghai` | ✅ | recommend_transport → get_weather | 4 | 13501 ms |
| `optimize-chengdu-day1` | ✅ | optimize_day_route | 5 | 9767 ms |
| `weather-then-route` | ✅ | get_weather → plan_route | 5 | 12170 ms |
| `plan-then-optimize` | ✅ | search_poi → search_poi → plan_trip → optimize_day_route | 5 | 32261 ms |
| `list-then-read` | ✅ | list_dir → read_file | 4 | 10345 ms |
