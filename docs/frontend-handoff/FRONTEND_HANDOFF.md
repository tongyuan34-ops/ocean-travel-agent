# 前端开发交接说明

> 这份文档是给「负责生成前端代码的 AI / 开发者」的输入材料。
> 后端已全部完成并实测通过；**你只需实现前端，不要改后端**。

---

## 1. 目标

为「旅行规划助手」实现 Web 前端。它不是普通聊天应用：后端会调用工具、
会请求权限、会产生媒体任务，前端必须把这些过程**可视化**，
让用户看得见 agent 正在做什么，并能在有副作用的操作前拦住它。

核心体验：

- 用户提问 → 对话流里实时出现**工具调用卡片**（正在查天气/搜网页/生成图片…）
- 有副作用的操作（写文件、删路径、换壁纸）→ **弹模态确认框**，用户点了才执行
- 生图为慢任务 → 先出占位卡片，前端**轮询**任务状态，完成后展示图片
- 回答里带来源链接 → 渲染为可点击外链

---

## 2. 技术栈（硬要求）

- **React 18 + TypeScript（strict 模式）**
- **Vite** 构建
- **不使用 UI 组件库**，手写 CSS；深色主题；适配桌面与窄屏
- 环境变量走 `.env.development` / `.env.production`（`VITE_API_BASE_URL`）
- 前端开发端口 **3001**（不是 3000 或 5173）
- Vite proxy：`/api` → `http://localhost:8000`

---

## 3. 后端接口地址

| 项 | 值 |
|---|---|
| 后端端口 | `8000` |
| API 前缀 | `/api/v1` |
| 开发时访问 | 经 Vite proxy 走 `/api/v1/...`（同源，无 CORS 问题） |
| 字符集 | UTF-8，`Content-Type: application/json` |
| 追踪头 | 建议每个请求带 `X-Request-Id`（uuid），后端会回显到日志 |

**可直接导入的机器可读契约**：`openapi.json`（本目录内，FastAPI 自动生成）。
**各接口真实响应样例**：`api_snapshot.json`（本目录内，从真实运行的后端导出）。

---

## 4. 接口清单

### 4.1 会话

```
POST   /api/v1/chat/sessions                    创建会话（请求体为空）
DELETE /api/v1/chat/sessions/{session_id}       删除会话 → 204
GET    /api/v1/chat/sessions/{session_id}/messages   拉取历史
```

创建响应：
```json
{ "session_id": "uuid", "created_at": "2026-10-20T13:00:00+00:00" }
```

历史响应：
```json
{ "items": [ { "id": "uuid", "role": "user", "content": "帮我规划三亚", "created_at": "..." } ] }
```

`role` 只有 `user` / `assistant` 两种。

### 4.2 发消息（SSE 流式，唯一路径）

```
POST /api/v1/chat/sessions/{session_id}/messages
Content-Type: application/json

{ "message": "帮我规划 5 天三亚行程，预算 8000" }
```

响应 `Content-Type: text/event-stream`。每条事件形如 `data: {单行 JSON}\n\n`。

**没有非流式接口**，必须实现 SSE。

### 4.3 工具清单

```
GET /api/v1/tools
```
```json
{ "items": [
    { "name": "get_weather", "category": "info", "enabled": true,
      "requires_permission": false, "description": "查询指定城市的天气。" }
] }
```

用途：渲染工具的中文名（`description` 给用户看，不要显示 `name`）；
`requires_permission=true` 的工具要提示用户"该操作会先请求授权"。

`category` 取值：`info` / `search` / `plan` / `media` / `system` / `plugin` / `other`。

### 4.4 权限确认

```
POST /api/v1/permissions/{request_id}/decision
{ "granted": true, "remember": false }
```

`remember=true` 表示本会话内同类操作不再询问。
**超时 120 秒未决策，后端按拒绝处理**，前端要提示"已自动拒绝"。

### 4.5 媒体生成（异步任务）

```
GET  /api/v1/media/templates        生图模板库
POST /api/v1/media/images           提交生图任务 → 202 { task_id, status }
GET  /api/v1/media/tasks/{task_id}  轮询任务状态
```

模板响应：
```json
{ "items": [ { "key": "checkin", "label": "旅行打卡", "scene": "...",
               "style": "...", "composition": "...", "lighting": "...",
               "example": "..." } ] }
```

任务状态：
```json
{ "task_id": "uuid", "status": "pending|running|succeeded|failed",
  "result_url": "https://...", "error": null }
```

轮询建议：2 秒起，逐步退避到 5 秒上限；进入 `succeeded` / `failed` 后停止。

**生视频接口存在但默认关闭**（`video_gen=false`），调用会返回 503；
前端必须依据 `capabilities.video_gen` 隐藏入口，不要硬编码显示。

### 4.6 能力探测

```
GET /api/v1/system/capabilities
```
```json
{ "image_gen": true, "video_gen": false,
  "filesystem": true, "wallpaper": true,
  "link_parse": true, "search_web": true,
  "search_social": true, "session_fetch": true,
  "platforms": ["amap","weather","bing_cn","web_fetch","douyin","bilibili"] }
```

**这是启动时必须调的第一个接口**，据此控制界面：

| 字段 | 为 false 时 |
|---|---|
| `image_gen` | 隐藏生图入口 |
| `video_gen` | 隐藏生视频入口 |
| `wallpaper` | 隐藏切换壁纸入口 |
| `filesystem` | 隐藏文件操作相关提示 |
| `search_web` | 隐藏通用网页检索入口 |
| `search_social` | 不展示社媒检索入口 |
| `session_fetch` | 不展示登录态采集入口 |
| `platforms` | 渲染「数据来源」筛选器与来源标签 |

> `platforms` 的取值是后端适配器名，**前端不得硬编码平台清单**，一律以接口返回为准。

### 4.7 健康检查

```
GET /api/v1/health   →  { status, version, model, provider }
```

---

## 5. SSE 事件协议（最容易写错的地方）

### 5.1 事件类型

```json
{"type":"token","content":"增量文本"}

{"type":"tool_call","call_id":"uuid","tool":"get_weather","category":"info","args":{"city":"三亚"}}

{"type":"tool_result","call_id":"uuid","status":"ok","summary":"简短结果摘要"}

{"type":"permission_request","request_id":"uuid","action":"write_file",
 "detail":{"path":"C:\\...\\行程.md","preview":"文件内容前 1200 字"}}

{"type":"end","message_id":"uuid","finish_reason":"stop|length"}

{"type":"error","code":"MODEL_TIMEOUT","message":"人类可读信息"}
```

### 5.2 真实事件流样例（一次含工具调用的完整问答）

```
data: {"type":"token","content":"我来查一下"}

data: {"type":"tool_call","call_id":"abc123","tool":"get_weather","category":"info","args":{"city":"三亚"}}

data: {"type":"tool_result","call_id":"abc123","status":"ok","summary":"三亚 天气预报（数据来源：高德开放平台）：白天多云 33℃…"}

data: {"type":"token","content":"三亚今天白天"}

data: {"type":"end","message_id":"msg-1","finish_reason":"stop"}

```

### 5.3 解析要求（必须全部满足）

1. 用 `fetch` + `ReadableStream` + `TextDecoder` **手工解析**，不要用 `EventSource`
   （需要 POST 请求体与主动中断）
2. 按 `data: ` 前缀逐行解析，**遇到空行视为一个事件结束**
3. **必须处理半包/粘包**：用缓冲区拼接，只有遇到换行才切分，
   不得假设每次 `read()` 都返回完整事件
4. 单个事件 JSON 解析失败只记日志，**不能中断整条流**
5. 用 `AbortController` 支持「停止生成」
6. 收到 `end` 时检查 `finish_reason`：为 `length` 要提示「回答已达长度上限，可能被截断」
7. `call_id` 用于把 `tool_result` 匹配回对应的 `tool_call` 卡片

### 5.4 工具卡片行为

- 收到 `tool_call`：插入卡片，显示**中文名**（查 `/api/v1/tools` 的 `description`）+ 参数摘要，
  状态「执行中」
- 收到相同 `call_id` 的 `tool_result`：更新为「已完成」/「失败」，展示 `summary`（截断到 200 字）
- `status=error` 时把 `summary` 作为失败原因展示
- `search` / `plugin` 组的 summary 里含「命中源数 / 各源命中数 / 耗时 / 是否降级」，
  建议以可展开的明细呈现
- 卡片默认折叠，点击展开看参数
- 同一轮内多次调用按顺序排列，形成「agent 的工作轨迹」
- 视觉上要与正式回答区分（浅底色 + 左侧竖线）

---

## 6. 错误处理

非流式错误统一为：
```json
{ "code": "SESSION_NOT_FOUND", "message": "会话不存在：xxx" }
```

流式错误走 SSE 的 `error` 事件，字段相同。

错误码 → HTTP 状态：
```
400 INVALID_REQUEST          403 PERMISSION_DENIED      403 PATH_NOT_ALLOWED
404 SESSION_NOT_FOUND        404 TASK_NOT_FOUND         422 VALIDATION_ERROR
429 RATE_LIMITED             501 TOOL_NOT_ENABLED       502 MODEL_UNAVAILABLE
502 MODEL_EMPTY_RESPONSE     502 MEDIA_UPSTREAM_ERROR   503 MEDIA_BACKEND_MISSING
503 SESSION_EXPIRED          504 MODEL_TIMEOUT          504 MEDIA_UPSTREAM_TIMEOUT
500 INTERNAL_ERROR
```

前端建议文案：
```
MODEL_UNAVAILABLE      → 「模型服务不可用，请检查网络或稍后重试」
MODEL_TIMEOUT          → 「模型响应超时，可重试」
MODEL_EMPTY_RESPONSE   → 「模型未返回内容，可重试」
PERMISSION_DENIED      → 「该操作未获授权，已取消」
PATH_NOT_ALLOWED       → 「该路径不在允许范围内」
TOOL_NOT_ENABLED       → 「该功能未启用或未配置」
MEDIA_BACKEND_MISSING  → 「未配置该功能的生成后端」
RATE_LIMITED           → 「操作过于频繁，请稍后重试」
```

错误码集中在 `api/client.ts` 归一化为中文，**组件层不直接判断 code**。

---

## 7. 国产安卓自带浏览器兼容（硬要求，不可延后）

目标用户在国内，入口以**安卓自带浏览器**（华为/小米/vivo/OPPO 自带、UC、QQ 浏览器）
与微信/QQ 内置浏览器为主。这些内核普遍落后于 Chrome，且"省流模式"会改写页面。

- **SSE 必须有降级路径**：`fetch + ReadableStream` 在部分国产内核上不完整。
  必须做特性探测，探测失败回退到「短轮询」，不得让整块功能失效
- **构建目标压低**：Vite `build.target` 设为 ES2017 或更低；
  老内核不认 `<script type="module">`，必要时接 `@vitejs/plugin-legacy`
- **特性探测**：`AbortController`、`TextDecoder`、`fetch` 都要探测，
  缺失走降级或 polyfill，不得直接调用
- **CSS 不依赖新特性**：`backdrop-filter`、`aspect-ratio`、flex `gap` 用 `@supports` 降级；
  不用 `100dvh`，改用 `height: 100%` + JS 修正
- **移动端视口**：`viewport-fit=cover` + 安全区变量；
  软键盘弹起时底部输入区不能被遮住（国产内核的 `position: fixed` 行为与 Chrome 不同）
- **省流模式容错**：图片 `onerror` 要有占位，不能因图片失败导致卡片塌掉

---

## 8. 交互细节

### 8.1 聊天

- 流式打字机效果；渲染时自动滚到底部
- 用户手动上滚时**暂停自动滚动**，右下角出现「回到底部」按钮
- 发送中禁用输入框，显示「正在思考…」
- Enter 发送、Shift+Enter 换行
- 助手消息渲染 Markdown
- 行程类回答（按天行程、预算表）尽量结构化渲染

### 8.2 权限确认

- 收到 `permission_request` **立即弹模态框**，展示 `action`（写文件/删路径/切换壁纸）
  与 `detail`（`path`、`preview`）
- 三个按钮：拒绝 / 允许本次 / 允许且本会话不再询问
- 点击后调 `POST /api/v1/permissions/{request_id}/decision`
- **必须阻断式**：未获决策前不得让对话流"静默跳过"该操作
- **绝不在界面展示 cookie、token 等敏感信息**

### 8.3 媒体

- 生图提交后先展示占位卡片（pending），轮询更新状态
- 成功：展示图片，提供「保存到本地」「重新生成」
- 失败：展示 `error` 文案与重试按钮；
  若错误码为 `MEDIA_BACKEND_MISSING`，提示「未配置生图后端」而不是泛化的「生成失败」
- **必须提示**：图片链接 24 小时内失效，需及时另存（这是后端返回的真实约束）
- 提供模板库入口：从 `GET /api/v1/media/templates` 拉模板，
  用户选模板 + 填要素即可生成

### 8.4 会话管理

- 左侧会话列表：新建 / 切换 / 删除
- 切换会话时用历史接口恢复
- 会话标题自动取首条用户消息前 20 字
- 多轮追问（「预算改成 8000」「多加一天」）只需正常发消息，**前端不得自行拼装上下文**

### 8.5 来源引用

- 正文中的 URL 渲染为可点击外链（`target="_blank" rel="noopener"`）
- 提供来源清单区：按 `source_kind` / `platform` 分组，
  展示标题、平台、点赞、作者、发布时间、原文链接

---

## 9. 工程结构建议

```
src/
  api/
    client.ts        # 统一请求封装：错误码解析、超时、中断
    types.ts         # 与接口一一对应的 TS 类型（字段名不得改写）
    chat.ts          # 会话、消息、SSE 流
    tools.ts         # 工具清单
    permissions.ts   # 权限裁决
    media.ts         # 媒体任务提交与轮询
    system.ts        # 能力探测、壁纸
  hooks/
    useChat.ts       # 发送/流式接收/中断/重试
    useCapabilities.ts
  components/
    Sidebar.tsx  ChatWindow.tsx  MessageList.tsx  MessageBubble.tsx
    InputBox.tsx  ErrorBanner.tsx  Markdown.tsx
    ToolCallCard.tsx  PermissionDialog.tsx  MediaPanel.tsx  SourceList.tsx
  App.tsx
```

质量要求：
- 所有组件 Props 有明确类型，**禁止 any**
- `api/types.ts` 字段名必须与本文档一致，不得自行改写
- API 层与 UI 层分离
- 不写内联魔法字符串：路径、事件类型、错误码集中定义常量
- **所有代码注释与界面文案使用中文**

---

## 10. 绝对不要做的事

1. **不要猜数据**。价格、班次、余票、营业时间等，后端若没给，前端不许自己补数字或写"约""大概"
2. **不要硬编码平台清单**，一律读 `capabilities.platforms`
3. **不要在能力未返回前启用入口**（`capabilities` 未加载时相关按钮不可点击）
4. **不要在前端展示 `.env`、cookie、token 等任何敏感信息**
5. **不要改后端接口**。若发现契约不足，在文档里记录并反馈，不要自行加字段
6. **不要用 `EventSource`**（不支持 POST 与中断）

---

## 11. 联调前置

后端启动（在项目根目录）：

```bash
uvicorn app.main:app --reload --port 8000
```

自检：`curl http://localhost:8000/api/v1/health` 应返回 `{"status":"ok",...}`

前端启动后访问 `http://localhost:3001`，确认：
1. 首屏调 `/api/v1/system/capabilities` 成功
2. 能新建会话并发出第一条消息
3. 能看到流式输出
4. 让它「最近三亚天气怎么样」应能看到工具卡片

---

## 12. 本目录包含的文件

| 文件 | 内容 |
|---|---|
| `FRONTEND_HANDOFF.md` | 本文件，前端实现说明 |
| `SPEC.md` | 完整项目规范（唯一事实来源，含能力清单与全部契约） |
| `openapi.json` | FastAPI 自动生成的机器可读契约，可直接喂给代码生成工具 |
| `api_snapshot.json` | 各接口**真实运行响应**，含错误响应样例 |

> 本包**不含任何密钥、Cookie 或用户数据**。故意不包含后端源码与会话数据。
