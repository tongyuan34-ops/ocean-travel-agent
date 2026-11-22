# ADR 0001：拆分 ChatService，把会话、上下文、运行时与纯函数移出 chat.py

- 状态：已采纳
- 日期：2026-11-21

## 背景

`app/services/chat.py` 拆分前约 1600 行，一个 `ChatService` 同时承担六件互不相关的事：

1. 会话 CRUD（增删查、列表投影、persona / skill 切换、用量查询）；
2. 提示词与上下文装配（附件块、文档摘录、历史正文、会话文件清单、摘要折叠、turn facts）；
3. SSE 流生命周期（start / subscribe / cancel / 心跳 / 并发闸门）；
4. 单轮编排（`_generate` 约 490 行，包含准备、跑图、自愈重跑、收尾）；
5. 模型运行时（图事件 → SSE 翻译、跨进程取消轮询）；
6. 纯函数工具（SSE 帧、工具分类、摘要截断、回答抽取、错误码映射）。

后果是：任何一处改动都要在同一个文件里跨十几个方法读上下文；路由为了列一个会话列表也得先拿到
一个会发起模型调用的对象；附件解析、摘要折叠这类出过错的分支无法脱离整个聊天服务做单测。

本次重构的目标是**行为零变化**，把 `chat.py` 收成「门面 + 编排」，其余职责落到命名清晰的模块。

## 决策

新增 5 个模块，`chat.py` 只保留流生命周期与单轮三段式编排。

| 文件 | 职责 | 行数（非空） |
|---|---|---|
| `app/services/sessions.py` | `SessionService`：create / get / save / delete / list_rows / usage / set_persona / set_skill；`SessionRow`、`session_title()`、`session_updated_at()`、`TITLE_LIMIT`、`now_iso()` | 131 |
| `app/services/turn_context.py` | `TurnContext`：附件块、附件元数据、历史正文、会话文件清单、文档提示、系统提示词、turn facts、摘要折叠、裁剪 | 357 |
| `app/services/turn_runtime.py` | `Attempt`、`TurnRun`、`run_attempt()`（图事件 → SSE）、`CANCEL_POLL_SEC` 与跨进程取消轮询 | 191 |
| `app/services/turn_events.py` | 纯函数：`sse()`、`as_text()`、`truncate()`、`summarize()`、`extract_answer()`、`tool_category()` 及常量 | 105 |
| `app/services/turn_errors.py` | 纯函数：`map_failure()`、`quota_message()`、`auth_message()` 及关键词表 | 64 |
| `app/services/chat.py`（改） | `ChatService`：`start_reply` / `subscribe` / `cancel_stream` / `_run_reply` / `_release_slot` / `_graph_for` / `_spawn` / `_within_budget`，加上 `_generate` 拆出的 `_prepare_turn` / `_converse` / `_finalize` | 847 |

`_generate` 从约 490 行收成约 35 行，只剩三段调用：

```python
setup = await self._prepare_turn(state, session_id, text, user_id, trace_id, attachments or [])
if setup is None:
    return
try:
    outcome = await self._converse(setup)
finally:
    for var, token in setup.tokens:
        var.reset(token)
    await setup.broker.cancel_all()
await self._finalize(setup, outcome, trace_id, started)
```

调用点同步调整：

- `app/main.py` 构造 `SessionService(app.state.session_store)` 放进 `app.state.sessions`，再注入 `ChatService`；
- `app/api/routes/chat.py` 的会话类端点走 `request.app.state.sessions`，流式端点仍走 `app.state.chat`；
- `app/api/routes/tools.py` 的 `tool_category` 改从 `turn_events` 导入。

## 边界规则

这些规则是本次拆分能保持行为不变的前提，后续改动也必须遵守：

1. **`ChatService` 不直接碰 `SessionStore`**，只通过 `SessionService` 读写会话。
2. **`TurnContext` 不发送任何 SSE 事件**：它只回答「这一轮该让模型看到什么」，输入 `ChatSession` + 本轮文本，
   输出 `TurnPrompt`（系统提示词、消息、参考图、历史正文）。
3. **`run_attempt` 不碰存储、不碰提示词、不碰摘要**：只依赖 `TurnRun`（图、事件日志、队列、trace）与消息列表。
   自愈重跑就是「再调用一次」。
4. **`turn_events` / `turn_errors` 只放纯函数**：不接收 `StreamState`，不访问网络，便于单测直接驱动。
5. **ContextVar 的 set / reset 都留在 `_generate` 这一层**：`_prepare_turn` 返回 `(var, token)` 列表，
   `_generate` 的 `finally` 统一 reset。放进任何子方法里，异常路径都会漏掉 reset，
   下一轮就会继承上一轮的工具预算与污点状态。
6. **持久化顺序不变**：用户消息先落库 → 调模型 → 用量结算 → 助手消息落库 → 回写 `active_groups` / `active_skills`。
7. **取消语义不变**：先写共享取消标记，再 cancel 本地 Task；`run_attempt` 每 `CANCEL_POLL_SEC` 轮询一次跨进程标记。
8. **错误映射顺序不变**：配额（quota）→ 鉴权（auth）→ 超时 / 连接 → INTERNAL。
9. **SSE 协议不变**：帧格式 `data: {...}\n\n`、`id:` 前缀、15s 心跳、事件顺序
   （`token` → `tool_call` → `tool_result` → `end` / `error`）。

## 被否方案

1. **只外移纯函数（最小改动）**：能降一点行数，但 `_generate` 仍是 490 行的单体，评审时还是要整段读。
   收益不足以覆盖一次重构的回归成本。
2. **用 mixin / 多继承拆 `ChatService`**：文件行数下降，但 `self._log`、`self._sessions` 之类的隐式依赖
   反而更难追踪，且 mypy 对 mixin 属性推断更弱。
3. **先在 `chat.py` 内部把 `_generate` 拆成三个方法，不外移**：这正是本次的第一步；继续留在同一文件里，
   会话 CRUD 与运行时仍会挤占阅读上下文，路由也仍然依赖聊天服务。
4. **引入 `ChatTurn` 对象承载全部状态，把编排做成状态机**：改动面远大于本次要验证的范围，
   且很容易把「顺序」变成「回调」，与「行为零变化」的目标冲突。
5. **顺手优化**（合并重复分支、改错误文案、调整重试次数）：明确不做。重构与行为变更混在一起，
   一旦测试红了无法判断是哪一类改动引起的。

## 后果

- **收益**：`chat.py` 从约 1600 行降到 928 行（847 非空、约 717 非注释），`_generate` 从约 490 行降到约 35 行；
  会话、上下文、运行时、纯函数四层各自可以被单独测试与替换；路由列会话不再依赖会发起模型调用的对象。
- **代价**：模块数增加（+5），跨模块阅读时需要多跳一次；`ChatService` 构造签名变更（第二参改为
  `sessions: SessionService`），属于破坏性内部变更，测试中的构造与 patch 目标需要机械调整。
- **未达目标的部分**：计划给 `chat.py` 定的目标是 ≤650 行，实际 928 行。原因是原有中文注释（约 130 行）
  与各分支的「为什么」记录全部保留——删注释换行数不是这次重构的目的。若要继续压缩，只能把编排再切碎，
  反而牺牲「一轮对话在一个文件里读完」的可读性，因此到此为止。
- **风险与缓解**：最大风险是纯搬移引入顺序差异。缓解方式是分段搬移、每段跑对应测试；
  一旦出现难以归因的差异，可以停在「5 个模块已拆、`_generate` 保持原样」的中间可交付状态。

## 验证

1. `pytest -q`：全量通过（946 条；仅机械调整了 imports、构造参数与 monkeypatch 目标，行为断言未改）。
2. `ruff check app tests`：全部通过；`ruff format --check app tests`：168 个文件格式一致。
3. `mypy app`：零错误（124 个源文件）。
4. `python -m evals.run --self-test`：脚本化模型的离线端到端链路仍可跑通。
5. 行数：`chat.py` 928 行（847 非空），5 个新模块各 64–402 行，职责单一。
6. 线上冒烟：重启 `app.standalone`（127.0.0.1:8765）后用真实模型跑一轮，收到 100 个 `token` 帧与正常
   `end` 事件（`finish_reason=stop`、`latency_ms=1543`、无 `warnings`），公网 tunnel 的
   `/api/v1/health` 同时正常。
