# 前端开发交接说明

> 给「负责实现前端的开发者 / AI」的输入材料。
> 后端已全部完成并实测通过；**你只需实现前端，不要改后端**。
>
> 本文件回答三件事：
> 1. 后端现在**真的能做什么**（用户视角的功能清单）
> 2. 前端要遵守的**接口契约**（字段名不得改写）
> 3. **运行环境与联调方式**（端口、依赖、启动命令）

---

## 1. 产品定位

「旅行规划助手」不是普通聊天应用。后端是一个会调用工具的 agent：

- 用户提问 → 对话流里实时出现**工具调用卡片**（查天气 / 搜攻略 / 生成图片…）
- 有副作用的操作（写文件、删路径、换壁纸）→ **弹模态确认框**，用户点了才执行
- 生图是慢任务 → 先出占位卡片，前端**轮询**任务状态，完成后展示图片

前端必须把这些过程**可视化**，让用户看得见 agent 在做什么，并能在有副作用的操作前拦住它。

---

## 2. 后端已实现的功能（前端需要覆盖的全部范围）

下表是**当前代码真实可用**的能力，不是规划。工具能不能用取决于 `.env` 配置，
所以前端必须以运行时 `GET /api/v1/system/capabilities` 与 `GET /api/v1/tools` 为准，
**不得硬编码"这个功能一定有"**。

### 2.1 能力总览

| 业务能力 | 后端工具 | 依赖配置 | 前端主要工作 |
|---|---|---|---|
| 天气查询 | `get_weather` | 高德 Key | 工具卡片 |
| 地点/酒店/商圈检索 | `search_poi` | 高德 Key | 工具卡片 + 可点地图链接 |
| 路线规划（驾车/步行/公交） | `plan_route` | 高德 Key | 工具卡片 |
| 预算估算 | `estimate_budget` | 无 | 结构化渲染预算表 |
| 单日路线优化 | `optimize_day_route` | 高德 Key | 工具卡片 / 行程表 |
| 多天行程规划 | `plan_trip` | 高德 Key | 结构化渲染按天行程 |
| 住宿推荐 | `recommend_stay` | 高德 Key | 酒店卡片 + 地图链接 |
| 交通方式建议 | `recommend_transport` | 高德 Key | 工具卡片 |
| 网页检索 | `search_web` | 必应 Key 可缺省 | 工具卡片 + 来源明细 |
| 网页正文抓取 | `fetch_url` | 无 | 工具卡片 |
| 分享链接解析 | `parse_shared_link` | 无 | 链接卡片（标题/封面/地点线索） |
| 文件沙箱读写 | `list_dir` / `read_file` / `write_file` / `create_dir` / `delete_path` | `ALLOWED_ROOTS` | **权限模态框** |
| 切换桌面壁纸 | `set_wallpaper` | `ALLOWED_ROOTS` + Windows | **权限模态框** |
| 生图 | `generate_image` / `list_image_templates` | 生图后端 | 模板选择器 + 轮询卡片 |
| 社媒登录态采集（默认关闭） | `search_social_session` / `generate_scraper` | 开关 + Cookie + MediaCrawler | 入口按能力隐藏 |

> 生视频**不在产品范围内**（`video_gen=false`）。接口存在但调用会 503，前端必须隐藏入口。

### 2.2 逐项说明（用户说什么 → 后端做什么 → 前端展示什么）

**行程规划**
- 用户：「帮我规划 5 天三亚，预算 8000，住大东海」
- 后端：`plan_trip` 编排景点、按天分组、估算耗时与花费，并返回高德导航链接
- 前端：把回答里的「第 1 天 / 第 2 天」渲染成结构化时间线；链接可点击
- 多轮修改（「预算改成 6000」「多加一天」）直接发新消息，**前端不要自己拼上下文**

**天气**
- 后端返回实况 + 未来几天预报，并标注数据来源
- 前端：工具卡片展示 `summary`；不要自己造温度或"大概"

**住宿 / 交通**
- `recommend_stay` 返回真实 POI 的酒店名、地址、星级、地图链接；
  **不提供实时价格与预订页**，前端不得展示"¥xxx/晚"这类数字
- `recommend_transport` 返回真实驾车里程与耗时 + 经验建议；
  **不提供班次、余票、票价**，只给官方查询入口

**网页检索**
- `search_web` 返回首行统计（命中源数 / 各源命中数 / 耗时 / 是否降级）
  + 若干条标题、链接、摘要
- 前端：统计行可折叠；降级或 mock 结果建议给出"仅供参考"标记

**分享链接解析**
- 用户粘贴抖音/小红书/B站等链接 → `parse_shared_link` 提取标题、文案、封面、疑似地点
- 前端：卡片展示封面 + 标题 + 地点标签；封面 `onerror` 要有占位

**文件沙箱**
- 只有落在 `ALLOWED_ROOTS` 内的路径才允许操作；越界直接报 `PATH_NOT_ALLOWED`
- 写文件/建目录/删除/换壁纸**一定先发 `permission_request`**，前端必须弹框
- 删除只支持文件与**空目录**，非空目录会失败，前端如实显示原因

**生图**
- 两种入口都支持：
  1. 对话里让 agent 调 `generate_image` → 后端只返回 `task_id`，**不会等图**
  2. 前端直接 `POST /api/v1/media/images` 提交
- 两条路径的产物都在 `GET /api/v1/media/tasks/{task_id}` 查
- 任务在内存中保留 `MEDIA_TASK_TTL_SEC`（默认 3600 秒），过期返回 `TASK_NOT_FOUND`
- 图片链接是**上游临时链接，24 小时内失效**，必须提示用户及时另存
- 模板库 `GET /api/v1/media/templates` 提供 6 个模板：
  `checkin`（打卡）/ `poster`（海报）/ `note`（手账）/ `food`（美食）/ `night`（夜景）/ `portrait`（人像）

**社媒登录态采集（默认关闭）**
- 只有同时满足「开关开启 + Cookie 已配置 + MediaCrawler 就位」时 `capabilities.search_social=true`
- 在此之前前端**不展示任何社媒采集入口**
- `generate_scraper` 只生成脚本文本，**绝不执行**；前端要明确提示"仅草案，不会运行"

### 2.3 明确不做的事（不要给前端留入口）

- 生视频（产品决定关闭）
- 机票/火车票/酒店比价与下单
- 实时酒店价格、库存、班次
- 在小程序/网页内直接登录抖音等平台抓数据

---

## 3. 技术栈（硬要求）

- **React 18 + TypeScript（strict 模式）**
- **Vite** 构建
- **不使用 UI 组件库**，手写 CSS；深色主题；适配桌面与窄屏
- 环境变量走 `.env.development` / `.env.production`（`VITE_API_BASE_URL`）
- 前端开发端口 **3001**（不是 3000 或 5173）
- Vite proxy：`/api` → `http://localhost:8000`

---

## 4. 运行环境与联调

### 4.1 后端

| 项 | 值 |
|---|---|
| Python | 3.13（项目自带 `.venv`） |
| Web 框架 | FastAPI + uvicorn |
| 端口 | `8000` |
| API 前缀 | `/api/v1` |
| 会话存储 | 默认 Redis（`STORAGE_BACKEND=redis`，需先启动 Redis）；开发可改 `memory` |
| 字符集 | UTF-8，`Content-Type: application/json` |
| CORS | 默认只放行 `http://localhost:3001` |

启动（项目根目录）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

健康检查：

```powershell
curl http://localhost:8000/api/v1/health
# {"status":"ok","version":"...","model":"...","provider":"..."}
```

> 如果 Redis 没启动，会话相关接口会失败（启动日志会明确报错）。
> 前端联调前先确认 health 返回 ok。

### 4.2 `.env` 变量（只列前端会感受到影响的）

| 变量 | 影响 |
|---|---|
| `MAP_API_KEY` | 天气/路线/POI/住宿/交通/行程是否可用 |
| `WEATHER_API_KEY` | 天气数据源 |
| `BING_SEARCH_API_KEY` | 有则走官方检索，无则回退 HTML 抓取 |
| `IMAGE_BACKEND` / `CUSTOM_IMAGE_*` | `capabilities.image_gen` |
| `VIDEO_BACKEND` | 本项目固定 `none` |
| `ALLOWED_ROOTS` | `capabilities.filesystem`，为空则文件类工具全部不可用 |
| `ENABLE_SESSION_FETCH` | `capabilities.session_fetch` |
| `CORS_ORIGINS` | 前端端口不是 3001 时要同步改 |

**前端永远不要读取、展示或打包 `.env` 内容。**

### 4.3 机器可读契约

- `openapi.json`：FastAPI 自动生成的完整契约，可直接喂给代码生成工具
- `api_snapshot.json`：各接口**真实运行响应**样例
- `SPEC.md`：完整项目规范（唯一事实来源）

---

## 5. 接口清单

### 5.1 会话

```
POST   /api/v1/chat/sessions                          创建会话（请求体为空）
DELETE /api/v1/chat/sessions/{session_id}             删除会话 → 204
GET    /api/v1/chat/sessions/{session_id}/messages    拉取历史
```

创建响应：
```json
{ "session_id": "uuid", "created_at": "2026-10-20T13:00:00+00:00" }
```

历史响应：
```json
{ "items": [ { "id": "uuid", "role": "user", "content": "帮我规划三亚", "created_at": "..." } ] }
```

`role` 只有 `user` / `assistant`。后端保留最近 **20 轮**，更早的内容会被折叠进摘要（对前端透明）。

### 5.2 发消息（SSE 流式，唯一路径）

```
POST /api/v1/chat/sessions/{session_id}/messages
Content-Type: application/json

{ "message": "帮我规划 5 天三亚行程，预算 8000" }
```

- `message`：1–4000 字符
- 响应 `Content-Type: text/event-stream`，每条事件形如 `data: {单行 JSON}\n\n`
- **没有非流式接口**，必须实现 SSE

### 5.3 工具清单

```
GET /api/v1/tools
```
```json
{ "items": [
    { "name": "get_weather", "category": "info", "enabled": true,
      "requires_permission": false, "description": "查询指定城市的天气。" }
] }
```

用途：
- 渲染工具的中文名（`description` 给用户看，不要显示 `name`）
- `enabled=false` 的工具不要给可点击入口，或显示"未配置"
- `requires_permission=true` 的工具要提示"该操作会先请求授权"

`category` 取值：`info` / `search` / `plan` / `media` / `system` / `plugin` / `other`。

### 5.4 权限确认

```
POST /api/v1/permissions/{request_id}/decision
{ "granted": true, "remember": false }
```

- `remember=true`：本会话内同类操作不再询问
- **超时 120 秒未决策，后端按拒绝处理**，前端要提示"已自动拒绝"
- 响应：`{ "request_id": "uuid", "granted": true }`

### 5.5 媒体生成（异步任务）

```
GET  /api/v1/media/templates        生图模板库
POST /api/v1/media/images           提交生图任务 → 202 { task_id, status }
POST /api/v1/media/videos           提交生视频任务（本项目必然 503）
GET  /api/v1/media/tasks/{task_id}  轮询任务状态
```

生图请求体：
```json
{ "prompt": "三亚海边的旅行打卡照", "style": "checkin", "ref_image": null }
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
任务默认保留 3600 秒，过期返回 `TASK_NOT_FOUND`。

### 5.6 能力探测

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

> `platforms` 是后端适配器名，**前端不得硬编码平台清单**，一律以接口返回为准。

### 5.7 健康检查

```
GET /api/v1/health   →  { status, version, model, provider }
```

---

## 6. SSE 事件协议（最容易写错的地方）

### 6.1 事件类型

```json
{"type":"token","content":"增量文本"}

{"type":"tool_call","call_id":"uuid","tool":"get_weather","category":"info","args":{"city":"三亚"}}

{"type":"tool_result","call_id":"uuid","status":"ok","summary":"简短结果摘要"}

{"type":"permission_request","request_id":"uuid","action":"write_file",
 "detail":{"path":"C:\\...\\行程.md","preview":"文件内容前 1200 字"}}

{"type":"end","message_id":"uuid","finish_reason":"stop|length"}

{"type":"error","code":"MODEL_TIMEOUT","message":"人类可读信息"}
```

### 6.2 真实事件流样例

```
data: {"type":"token","content":"我来查一下"}

data: {"type":"tool_call","call_id":"abc123","tool":"get_weather","category":"info","args":{"city":"三亚"}}

data: {"type":"tool_result","call_id":"abc123","status":"ok","summary":"三亚 天气预报（数据来源：高德开放平台）：白天多云 33℃…"}

data: {"type":"token","content":"三亚今天白天"}

data: {"type":"end","message_id":"msg-1","finish_reason":"stop"}
```

### 6.3 解析要求（必须全部满足）

1. 用 `fetch` + `ReadableStream` + `TextDecoder` **手工解析**，不要用 `EventSource`
   （需要 POST 请求体与主动中断）
2. 按 `data: ` 前缀逐行解析，**遇到空行视为一个事件结束**
3. **必须处理半包/粘包**：用缓冲区拼接，只有遇到换行才切分，
   不得假设每次 `read()` 都返回完整事件
4. 单个事件 JSON 解析失败只记日志，**不能中断整条流**
5. 用 `AbortController` 支持「停止生成」
6. 收到 `end` 时检查 `finish_reason`：为 `length` 要提示「回答已达长度上限，可能被截断」
7. `call_id` 用于把 `tool_result` 匹配回对应的 `tool_call` 卡片

### 6.4 工具卡片行为

- 收到 `tool_call`：插入卡片，显示**中文名**（查 `/api/v1/tools` 的 `description`）+ 参数摘要，
  状态「执行中」
- 收到相同 `call_id` 的 `tool_result`：更新为「已完成」/「失败」，展示 `summary`（截断到 200 字）
- `status=error` 时把 `summary` 作为失败原因展示
- `search` / `plugin` 组的 summary 首行含「命中源数 / 各源命中数 / 耗时 / 是否降级」，
  建议以可展开的明细呈现
- 卡片默认折叠，点击展开看参数
- 同一轮内多次调用按顺序排列，形成「agent 的工作轨迹」
- 视觉上要与正式回答区分（浅底色 + 左侧竖线）

> 注意：SSE 目前**只提供文本 `summary`**，没有结构化的 `sources` 数组。
> 前端不得假设存在该字段，也不要自行从文本里解析出"点赞数"之类的数字来展示。

---

## 7. 错误处理

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
TASK_NOT_FOUND         → 「任务不存在或已过期」
RATE_LIMITED           → 「操作过于频繁，请稍后重试」
```

错误码集中在 `api/client.ts` 归一化为中文，**组件层不直接判断 code**。

---

## 8. 国产安卓自带浏览器兼容（硬要求，不可延后）

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

## 9. 交互细节

### 9.1 聊天

- 流式打字机效果；渲染时自动滚到底部
- 用户手动上滚时**暂停自动滚动**，右下角出现「回到底部」按钮
- 发送中禁用输入框，显示「正在思考…」
- Enter 发送、Shift+Enter 换行
- 助手消息渲染 Markdown
- 行程类回答（按天行程、预算表）尽量结构化渲染

### 9.2 权限确认

- 收到 `permission_request` **立即弹模态框**，展示 `action`（写文件/建目录/删路径/切换壁纸）
  与 `detail`（`path`、`preview`）
- 三个按钮：拒绝 / 允许本次 / 允许且本会话不再询问
- 点击后调 `POST /api/v1/permissions/{request_id}/decision`
- **必须阻断式**：未获决策前不得让对话流"静默跳过"该操作
- **绝不在界面展示 cookie、token 等敏感信息**
- 弹框要有 120 秒倒计时提示，超时后自动关闭并提示"已自动拒绝"

### 9.3 媒体

- 生图提交后先展示占位卡片（pending），轮询更新状态
- 成功：展示图片，提供「保存到本地」「重新生成」
- 失败：展示 `error` 文案与重试按钮；
  若错误码为 `MEDIA_BACKEND_MISSING`，提示「未配置生图后端」而不是泛化的「生成失败」
- **必须提示**：图片链接 24 小时内失效，需及时另存
- 提供模板库入口：从 `GET /api/v1/media/templates` 拉模板，
  用户选模板 + 填要素即可生成

### 9.4 会话管理

- 左侧会话列表：新建 / 切换 / 删除
- 切换会话时用历史接口恢复
- 会话标题自动取首条用户消息前 20 字
- 多轮追问（「预算改成 8000」「多加一天」）只需正常发消息，**前端不得自行拼装上下文**

### 9.5 来源引用

- 正文中的 URL 渲染为可点击外链（`target="_blank" rel="noopener"`）
- 工具卡片里的链接（高德地图、酒店位置、原文）同样可点击
- 当前没有结构化 sources 字段，来源信息只来自 `summary` 文本

---

## 10. 工程结构建议

```
src/
  api/
    client.ts        # 统一请求封装：错误码解析、超时、中断
    types.ts         # 与接口一一对应的 TS 类型（字段名不得改写）
    chat.ts          # 会话、消息、SSE 流
    tools.ts         # 工具清单
    permissions.ts   # 权限裁决
    media.ts         # 媒体任务提交与轮询
    system.ts        # 能力探测
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

## 11. 绝对不要做的事

1. **不要猜数据**。价格、班次、余票、营业时间等，后端若没给，前端不许自己补数字或写"约""大概"
2. **不要硬编码平台清单**，一律读 `capabilities.platforms`
3. **不要在能力未返回前启用入口**（`capabilities` 未加载时相关按钮不可点击）
4. **不要在前端展示 `.env`、cookie、token 等任何敏感信息**
5. **不要改后端接口**。若发现契约不足，在文档里记录并反馈，不要自行加字段
6. **不要用 `EventSource`**（不支持 POST 与中断）
7. **不要假设存在 `sources` 结构化字段**（当前只有文本 `summary`）

---

## 12. 联调自检清单

后端启动后：

```powershell
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/system/capabilities
curl http://localhost:8000/api/v1/tools
```

前端启动后访问 `http://localhost:3001`，依次确认：

1. 首屏调 `/api/v1/system/capabilities` 成功
2. 能新建会话并发出第一条消息
3. 能看到流式输出（token 逐段出现）
4. 问「三亚最近天气怎么样」应看到 `get_weather` 工具卡片
5. 让它写一个文件到沙箱目录，应弹出权限模态框
6. 提交一次生图，应看到 pending → succeeded 的轮询变化

---

## 13. 本目录包含的文件

| 文件 | 内容 |
|---|---|
| `FRONTEND_HANDOFF.md` | 本文件：功能清单 + 契约 + 环境 |
| `SPEC.md` | 完整项目规范（唯一事实来源） |
| `openapi.json` | FastAPI 自动生成的机器可读契约 |
| `api_snapshot.json` | 各接口**真实运行响应**示例 |

> 本包**不含任何密钥、Cookie 或用户数据**。
