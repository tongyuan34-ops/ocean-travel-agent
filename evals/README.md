# 评测

`tests/` 验的是**代码没写错**，`evals/` 验的是**模型没做错**。

这两件事的失败含义完全不同：单测红了说明你改坏了代码；评测红了可能只是换了模型、调了 prompt。
混在一个套件里会让人分不清是哪种，所以这里单独放。

## 怎么跑

```bash
# 1) 离线自测：只验证评测框架本身，不花钱、不联网（CI 里跑这个）
uv run python -m evals.run --self-test

# 2) 用真实模型跑用例集，出报告
uv run python -m evals.run --out evals/report.md --json evals/report.json

# 3) 卡阈值：通过率低于 80% 就以非零码退出，可直接接进发版流程
uv run python -m evals.run --fail-under 0.8

# 常用开关
#   --limit 5          只跑前 5 条，调 prompt 时快速迭代
#   --no-judge         跳过 LLM-as-judge，只看工具选择（便宜很多）
#   --mode structured  强制 structured 模式，验证小模型路径
#   --timeout 120      单条用例超时
```

报告会写到 `evals/report.md`（人看）和 `evals/report.json`（机器看，便于做趋势对比）。

## 用例怎么写

```yaml
- id: weather-beijing          # 唯一，重名会直接报错
  category: tool_selection     # 用于分类统计
  input: 北京明天天气怎么样？    # 用户原话
  expect_tools: [get_weather]  # 必须调用的工具
  forbid_tools: [search_web]   # 绝对不能调用的工具
  ordered: false               # true 则要求按顺序出现（子序列匹配）
  must_mention: [北京]          # 回答里必须出现的关键信息
  rubric: 应给出天气状况与温度区间，并说明数据来自天气接口而非编造。
```

四个断言各抓一类问题：

| 断言 | 抓什么 |
|---|---|
| `expect_tools` | 该调的工具没调 |
| `forbid_tools` | 该用专用工具却去搜网页 |
| `must_mention` | 回答缺关键信息 |
| `rubric` | 交给 LLM-as-judge 打 1-5 分 |

`rubric` 为空时用通用标准。打分锚点见 `runner.py` 里的 `_JUDGE_PROMPT`：5 分要求事实准确且无编造，
2 分以下意味着出现了「未经工具验证的具体数字」或「声称做了实际没做的事」。

### 关于 `self_test`

带 `self_test.script` 的用例可以用脚本化模型离线跑。**不要为了凑数给所有用例编剧本**——
那样自测会「全绿」，但实际什么都没验证。剧本只用来证明框架能正确观测到工具序列与用量，
不证明模型表现。

## 指标怎么读

| 指标 | 含义 | 怎么用 |
|---|---|---|
| 通过率 | 四项断言全过的比例 | 主指标，看趋势 |
| 工具召回率 | 期望工具被调到的比例 | 只统计声明了 `expect_tools` 的用例，避免被无期望用例稀释 |
| 误调数 | 调用了 `forbid_tools` 的用例数 | 这个比通过率更值得盯：误调往往意味着 prompt 里的工具描述有歧义 |
| 平均质量分 | judge 打分的均值 | 只在真模型模式下有值 |
| P50 / P95 延迟 | 端到端耗时 | 加工具后 P95 会明显上升，是「变慢」的早期信号 |
| 总 token / 成本 | 本次评测的总开销 | 价目表匹配不到模型时成本为 `null`，不是 0 |

## 加一条用例的判断标准

只加**能失败**的用例。一条永远会通过的用例只是噪音。
写之前先问：如果我故意把 prompt 里某条规则删掉，这条用例会不会红？不会就说明它没测到东西。
