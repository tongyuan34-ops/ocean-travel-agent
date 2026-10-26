# 基线解读

`evals/report.md` 是最近一次全量评测的原始产物（每次跑都会覆盖）。这份文档记录**怎么读它**，
以及基线暴露出的问题——包括用例本身的问题。

跑评测的模型：`deepseek-v4-1-flash-economy`（火山方舟）。环境：高德 Key 已配，
必应搜索 Key 未配，生图后端为 `custom`。

## 两轮结果

| 指标 | 首轮（用例有缺陷） | 修正后 |
|---|---|---|
| 通过 | 16 / 31（51.6%） | 20 / 31（64.5%） |
| 工具召回率 | 69.2% | 73.9% |
| 误调禁用工具 | 0 例 | 0 例 |
| 平均质量分 | 3.55 | 3.81 |
| 延迟 P50 / P95 | 11.4s / 75.4s | 11.4s / 90.0s |

分类通过率的变化比总数更能说明问题：

| 分类 | 首轮 | 修正后 | 说明 |
|---|---|---|---|
| permission | 0 / 3 | **3 / 3** | 断言方式改对了，不是 agent 变好了 |
| tool_selection | 4 / 7 | 4 / 7 | 没动，说明问题不在用例 |
| multi_tool | 5 / 9 | 5 / 9 | 同上 |
| tool_routing | 1 / 4 | 2 / 4 | 一半是超时，一半是方差 |

## 第一类：用例自己写错了（已修）

首轮 15 条失败里有 6 条是用例的问题。这类必须修，否则指标没有意义。

**1. rubric 与 SYSTEM_PROMPT 冲突**

`SYSTEM_PROMPT` 第 3 条明确写着「回复中不要标注『数据来源：高德开放平台』之类的来源标签」，
但三条 rubric 却要求「说明数据来自天气接口」。模型遵守了 prompt，评审却因此判它 2 分。

评审只能看到「问题 + 回答 + rubric」，它不知道 prompt 里禁了什么。所以 rubric 必须
和 prompt 保持一致，否则评的是幻觉中的规范。

**2. 权限用例断言了错误的层**

原来写 `expect_tools: [write_file]`，但模型合理的做法是先 `list_dir` 找到桌面真实路径，
再决定写哪里。断言工具名等于把「先探路」判成失败。

改成断言**有没有撒谎**：用户没确认时不能说「已写入」。工具名由单测覆盖
（`test_permissions.py` 有 12 条），评测不必重复。修完 permission 从 0/3 变 3/3，
说明首轮的失败确实全部来自断言方式。

**3. 用例输入依赖了不存在的上下文**

`wallpaper-needs-confirm` 原输入是「帮我把壁纸换成**刚才那张**洱海图」——评测里没有「刚才」，
模型只能翻遍目录找图，8 次工具调用全是 `list_dir` / `read_file` / `search_web`。
已改成自带完整路径的输入。

## 第二类：真实的 agent 行为问题

这部分改 rubric 消不掉，是下一步要动 prompt 或代码的地方。

**1. 工具抖动（thrashing）**

同一个工具被反复调用，参数只做微调：

```
route-ambiguous-place   plan_route → recommend_transport → plan_route → search_poi ×2 → plan_route
trip-budget-xiamen      search_poi ×3 → search_web ×3 → fetch_url → search_web → ...
search-degraded         search_web ×4 → fetch_url → search_web → fetch_url
file-read-readme        list_dir ×4 → read_file
```

代价直接体现在延迟上：这几条分别花了 46s / 63s / 28s / 12s。

根因大概是 `SYSTEM_PROMPT` 第 4 条让模型「改用更具体的地名重试」，但没给重试次数上限。
下一步应该在 prompt 里加「同一工具最多重试 2 次，之后如实告知」。

**2. 超时（4 条）**

`trip-chongqing-2d`、`trip-hangzhou-3d`、`social-douyin-search`、`image-generate-erhai`
全部撞到 90 秒硬上限。这类「不报错、只是很慢」的失败在线上表现为用户干等，
比报错更伤体验——因为前端连错误提示都给不出。

`social-douyin-search` 和 `image-generate-erhai` 走的是外部采集与生图后端，
慢是上游问题；但前两条是纯规划类，慢只能归因于工具循环。

**3. 工具失败时会编造**

`weather-not-enabled` 两轮都是 2 分：工具返回了错误，模型仍然给出具体温度和风力。
这直接违反 `SYSTEM_PROMPT` 第 1 条「工具失败时如实告知，不要猜测」。

值得记住的是：**prompt 里写了规则，不等于模型会遵守**。没有评测，这条规则只是自我安慰。

**4. 不该调工具时调了**

`optimize-chengdu-day1` 调了 `list_dir`——用户问的是「优化成都第一天路线」，
和文件系统毫无关系。这属于工具选择噪音，说明 22 个工具的 description 里
`list_dir` 的边界不够清楚。

## 第三类：环境差异（不是缺陷）

- 必应搜索 Key 未配置时 `search_web` 会走降级路径，相关用例的通过与否取决于 Key 状态；
- 生图上游失败时模型如实说明失败，按新 rubric 可以接受。

这两类会让同一份用例在不同机器上结果不同。所以报告里的 `label` 必须写清模型与配置，
否则数字没有可比性。

## 第四类：评测本身的方差

两轮之间有三条用例翻转，而**代码和 prompt 都没变**：

| 用例 | 首轮 | 修正后 | 变化原因 |
|---|---|---|---|
| `route-ambiguous-place` | ❌ 调 1 次工具 | ✅ 调 6 次工具 | 模型这一轮更愿意反复核对 |
| `list-then-read` | ❌ 调 5 次 | ✅ 调 2 次 | 这一轮没陷入抖动 |
| `image-generate-erhai` | ❌ 判 1 分 | ❌ 超时 | 失败方式都变了 |

结论：**31 条样本的单次通过率波动约 ±10 个百分点**。所以：

- 比较改动时看**分类趋势**，不要看总数差 1-2 条就下结论；
- 单次结果不足以证明「变好了」，要跑 3 轮取中位数，或者看 pass@3；
- 这也是为什么 CI 里只跑 `--self-test`（验证框架），不跑真实评测——
  一个会随机的数字卡在每次提交上只会制造噪音。

## 怎么用这份基线

1. **先看误调数**（`forbid_tools` 命中）。它比通过率更值得盯：误调说明工具描述有歧义，
   而漏调可能只是模型保守。
2. **再看延迟 P95**。加工具、改 prompt 之后 P95 会先涨，这是「变慢」最早的信号。
3. **改动前后各跑一次，只看差值**，并且接受 ±10% 的噪声。

调 prompt 时的推荐循环：

```bash
uv run python -m evals.run --limit 8 --no-judge    # 快速迭代，只看工具选择
uv run python -m evals.run --out evals/report.md   # 定稿前跑全量，带质量分
```
