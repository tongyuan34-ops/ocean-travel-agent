# 旅行规划助手 · 后端上下文（精简版）

> 用途：给实现前端或接手项目的 AI 提供**当前真实生效**的契约与运行语义。
> 源码以仓库为准，本文件不再整段粘贴代码，只保留会影响调用方的细节。
> 冲突时优先级：实际代码 > 本文件 > `docs/SPEC.md`（历史规范）。

---

## 1. 产品与能力

后端是会调用工具的 agent：回答过程中实时推送「工具调用卡片」，有副作用的操作先弹确认框，生图是异步任务由前端轮询。

| 业务能力 | 工具 | 依赖 | 前端工作 |
|---|---|---|---|
| 天气 | `get_weather` | 高德 Key | 工具卡片 |
| POI / 酒店 / 商圈 | `search_poi` | 高德 Key | 卡片 + 可点地图链接 |
| 路线（驾车/步行/公交） | `plan_route` | 高德 Key | 工具卡片 |
| 预算估算 | `estimate_budget` | 无 | 结构化预算表 |
| 单日路线优化 | `optimize_day_route` | 高德 Key | 卡片 / 行程表 |
| 多天行程 | `plan_trip` | 高德 Key | 按天时间线 |
| 住宿 | `recommend_stay` | 高德 Key | 酒店卡片 + 地图链接 |
| 交通建议 | `recommend_transport` | 高德 Key | 工具卡片 |
| 网页检索 | `search_web` | 必应 Key 可缺省 | 卡片 + 来源明细 |
| 正文抓取 | `fetch_url` | 无 | 工具卡片 |
| 分享链接解析 | `parse_shared_link` | 无 | 链接卡片 |
| 文件沙箱 | `list_dir` / `read_file` / `open_file` / `write_file` / `create_dir` / `delete_path` | `ALLOWED_ROOTS` | **权限确认框** |
| 换壁纸 | `set_wallpaper` | `ALLOWED_ROOTS` + Windows | **权限确认框** |
| 生图 | `generate_image` / `list_image_templates` | 生图后端 | 模板选择 + 轮询卡片 |
| 社媒采集 | `search_social_session` / `generate_scraper` | 开关 + Cookie + MediaCrawler | 按能力隐藏入口 |

能力必须**运行时探测**，不得硬编码：`GET /api/v1/system/capabilities` 与 `GET /api/v1/tools` 才是准绳。

**不做**：生视频（`video_gen=false`，接口 503）、机票/火车票/酒店比价下单、实时房价库存班次、站内登录平台抓数据。

`requires_permission=true` 的工具：`open_file`、`write_file`、`create_dir`、`delete_path`、`set_wallpaper`、`run_script`。

---

## 2. 技术栈与运行

| 项 | 值 |
|---|---|
| 后端 | Python 3.13 / FastAPI / uvicorn，端口 **8000**，前缀 `/api/v1` |
| 前端 | **原生静态页**（HTML/CSS/JS，无构建、无 React/Vite），文件在 `web/`，端口 **3001** |
| 前端入口 | `web/serve.py`：静态托管 + `/api` 同源反代 + Basic Auth（`WEB_AUTH_USER` / `WEB_AUTH_PASS`，留空不启用；本机 localhost 免密） |
| 会话存储 | 默认 Redis（`STORAGE_BACKEND=redis`），可切 `memory`（重启即丢） |
| CORS | 默认仅 `http://localhost:3001` |
| 约束 | 后端只能单 worker：SSE 流状态在进程内存 |

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
D:\develop\python\python.exe web\serve.py --port 3001
```

健康检查：`GET /api/v1/health` → `{status, version, model, provider}`。Redis 未启动时后端能起，但会话接口失败（启动日志会报）。

### 前端现状（早期交接文档写的是 React+Vite，已过时）

- 实现：`web/index.html` + `web/app.js` + `web/styles.css`；`web/assets/` 放图标与 3 张预设壁纸
- 入口：`web/serve.py` 提供静态文件与 `/api` 同源代理；`/share/` 路径免登录，其余路径走 Basic Auth，本机 localhost 预览免密
- PWA：`web/manifest.webmanifest` 声明 `standalone`，手机可「添加到主屏幕」；iOS Safari 不支持元素级全屏时自动隐藏全屏按钮
- 会话：ID 列表存 localStorage，消息在服务端；加载历史时按消息里的 `media_task_ids` 重建图片卡片
- 流式：`fetch` + `ReadableStream` 手工解析 SSE；工具卡片、权限模态框、媒体轮询（走 `/media/tasks/{id}/image`）
- 其他：预设/本地图片壁纸、桌面与移动端全屏、窄屏适配（含国产安卓浏览器）

### 关键 `.env`（只列影响功能的）

| 变量 | 作用 / 默认 |
|---|---|
| `MODEL_PROVIDER` `MODEL_NAME` `MODEL_BASE_URL` `MODEL_API_KEY` | 模型接入；方舟/DeepSeek 会自动注入 `thinking:disabled` |
| `MAX_CONTEXT_TOKENS` | 上下文预算，默认 4096 |
| `IMAGE_BACKEND` / `CUSTOM_IMAGE_*` | `custom` 时直连火山方舟 Images API，`capabilities.image_gen` 同源 |
| `VIDEO_BACKEND` | 固定 `none` |
| `MEDIA_TASK_TTL_SEC` | 媒体任务保留秒数；默认 3600，本项目 86400 |
| `STORAGE_BACKEND` `REDIS_URL` `SESSION_TTL_SEC` | 会话存储；`SESSION_TTL_SEC=0` 表示不过期 |
| `ALLOWED_ROOTS` | 文件/壁纸工具白名单，空则全部不可用 |
| `MAP_API_KEY` `WEATHER_API_KEY` `BING_SEARCH_API_KEY` | 天气/路线/POI/检索是否可用 |
| `ENABLE_SESSION_FETCH` `COOKIE_DIR` `MEDIACRAWLER_PATH` `CRAWLER_PLATFORMS` | 社媒采集开关与依赖 |
| `SOURCE_TIMEOUT_SEC` `FETCH_TIMEOUT_SEC` `MAX_FETCH_BYTES` | 检索超时 20s / 抓取 15s / 1.5MB |
| `CORS_ORIGINS` | 前端端口变化时同步改 |

前端不得读取、展示或打包 `.env`；密钥只存在服务端。

---

## 3. 接口清单

### 3.1 会话与聊天

```
POST   /api/v1/chat/sessions                         创建会话（空请求体）
DELETE /api/v1/chat/sessions/{session_id}            删除 → 204
GET    /api/v1/chat/sessions/{session_id}/messages   拉历史
POST   /api/v1/chat/sessions/{session_id}/messages   发消息 → SSE
GET    /api/v1/chat/streams/{stream_id}              断线续传（Last-Event-ID）
POST   /api/v1/chat/streams/{stream_id}/cancel       停止生成
```

```json
// 创建响应
{"session_id":"uuid","created_at":"2026-10-20T13:00:00+00:00"}

// 历史响应（media_task_ids 用于重建图片卡片）
{"items":[{"id":"uuid","role":"user","content":"...","created_at":"...","media_task_ids":[]}]}

// 发消息请求体
{"message":"帮我规划 5 天三亚，预算 8000"}
```

- `message` 长度 1–4000；**只有 SSE，没有非流式接口**
- 响应头带 `X-Stream-ID`，前端保存它用于断线续传
- 历史保留最近 20 轮（40 条），更早折叠进摘要，对前端透明
- 停止生成**不会**保存半截回答

### 3.2 工具 / 权限 / 能力

```
GET  /api/v1/tools
GET  /api/v1/system/capabilities
GET  /api/v1/health
POST /api/v1/permissions/{request_id}/decision
GET  /api/v1/memory                              当前设备的长期偏好
POST /api/v1/memory/items                        确认后写入偏好
DELETE /api/v1/memory/items/{item_id}            删除一条
DELETE /api/v1/memory                            清空全部
GET  /api/v1/memory/suggestions                  待确认的候选
DELETE /api/v1/memory/suggestions                忽略候选
```

```json
// tools
{"items":[{"name":"get_weather","category":"info","enabled":true,
           "requires_permission":false,"description":"查询指定城市的天气。"}]}

// capabilities
{"image_gen":true,"video_gen":false,"filesystem":true,"wallpaper":true,
 "link_parse":true,"search_web":true,"search_social":true,"session_fetch":true,
 "platforms":["amap","weather","bing_cn","web_fetch","douyin","bilibili"]}

// 权限裁决
{"granted":true,"remember":false}
```

- `category` 取值：`info` / `search` / `plan` / `media` / `system` / `plugin` / `other`
- 工具中文名取 `description`，不要展示 `name`
- `remember=true`：本会话内同类操作不再询问
- 权限请求 **120 秒**未裁决按拒绝处理，前端要提示「已自动拒绝」
- `platforms` 不得硬编码，一律读接口

### 3.3 用户偏好（长期记忆）

所有 memory 接口按**设备**隔离：前端在 localStorage 生成 UUID，每个请求带 `X-Device-ID`；
缺失时后端落到 `default`。

```json
// GET /memory 与 POST /memory/items 的响应
{"items":[{"id":"uuid","category":"饮食","text":"不吃辣","created_at":"..."}],
 "updated_at":"2026-10-24T07:00:00+00:00"}

// 写入请求（用户确认后）
{"items":[{"category":"饮食","text":"不吃辣"}]}

// 候选
{"items":[{"id":"uuid","category":"节奏","text":"行程别太赶"}]}
```

- 抽取在每轮回答落库后**后台**跑，命中偏好信号词才触发，不阻塞 SSE
- 抽取结果只作为候选，**用户确认后才写入**；`POST /items` 后候选自动清空
- 正式偏好最多 20 条，按文本判重；注入提示词时最多 12 条 / 600 字
- 前端在每次 `end` 后轮询一次 `/memory/suggestions`，有候选才弹确认框
- 设置面板展示全部偏好，可逐条删除或清空

### 3.4 媒体（异步任务）

```
GET  /api/v1/media/templates
POST /api/v1/media/images                    → 202 {task_id, status}
POST /api/v1/media/videos                    → 本项目必然 503
GET  /api/v1/media/tasks/{task_id}
GET  /api/v1/media/tasks/{task_id}/image    ?download=true 可下载
```

```json
// 提交生图
{"prompt":"三亚海边的旅行打卡照","style":"checkin","ref_image":null}

// 任务状态
{"task_id":"uuid","status":"pending|running|succeeded|failed",
 "result_url":"https://...","error":null}

// 模板项
{"key":"checkin","label":"旅行打卡","scene":"...","style":"...",
 "composition":"...","lighting":"...","example":"..."}
```

- 6 个模板：`checkin` 打卡 / `poster` 海报 / `note` 手账 / `food` 美食 / `night` 夜景 / `portrait` 人像
- 轮询：2 秒起退避到 5 秒，`succeeded`/`failed` 后停止
- 任务过期返回 `TASK_NOT_FOUND`
- `/image` 接口：先读 `media/` 本地成品，再代理上游临时链接并缓存；前端**必须用这个接口显示图片**（上游链接 24 小时失效且跨域）

### 3.5 行程导出

```
GET  /api/v1/trips/map?stops=西湖&stops=灵隐寺&city=杭州&origin=...   → image/png
POST /api/v1/trips/export                                            → A4 PDF
POST /api/v1/trips/export/ics                                        → text/calendar
POST /api/v1/trips/share                                             → {path, expires_at}
GET  /share/{token}                                                  → 只读分享页（免登录）
```

```json
{"title":"我的行程","city":"杭州","origin":"","mode":"driving","start_date":"2026-11-01",
 "days":[{"date":"","title":"","stops":[{"name":"西湖","time":"09:00","note":""}]}]}
```

上限：15 天、每天 12 个地点；地图失败仍会出 PDF/日历，不阻断。

---

## 4. SSE 事件协议

```json
{"type":"token","content":"增量文本"}
{"type":"tool_call","call_id":"uuid","tool":"get_weather","category":"info","args":{"city":"三亚"}}
{"type":"tool_result","call_id":"uuid","status":"ok|error","summary":"200 字内摘要"}
{"type":"permission_request","request_id":"uuid","action":"write_file",
 "detail":{"path":"C:\\...\\行程.md","preview":"前 1200 字"}}
{"type":"end","message_id":"uuid","finish_reason":"stop|length"}
{"type":"error","code":"MODEL_TIMEOUT","message":"人类可读信息"}
{"type":"ping"}
```

`ping` 是第 15 秒起的保活心跳（工具执行期间没有 token），前端**忽略**即可。

解析硬要求：

1. 用 `fetch` + `ReadableStream` + `TextDecoder` 手工解析，**不要用 `EventSource`**（不支持 POST 与中断）
2. 按 `data: ` 前缀切行，**空行表示一个事件结束**
3. 必须处理半包/粘包：用缓冲区拼接到换行再切分
4. 单条 JSON 解析失败只记日志，不中断整条流
5. `AbortController` 支持停止；`finish_reason=length` 提示回答可能被截断
6. 用 `call_id` 把 `tool_result` 匹配回对应的 `tool_call` 卡片
7. SSE **只有文本 `summary`**，不存在结构化 `sources` 字段

工具卡片：默认折叠，显示中文名 + 参数摘要；`status=error` 时把 `summary` 当失败原因；`search`/`plugin` 组首行含命中源数/耗时/降级标记，可展开；视觉上要与正文区分。

---

## 5. 错误处理

```json
{"code":"SESSION_NOT_FOUND","message":"会话不存在：xxx"}
```

流式错误走 SSE 的 `error` 事件，字段相同。

| HTTP | 错误码 |
|---|---|
| 400 | `INVALID_REQUEST` |
| 403 | `PERMISSION_DENIED`、`PATH_NOT_ALLOWED` |
| 404 | `SESSION_NOT_FOUND`、`TASK_NOT_FOUND` |
| 422 | `VALIDATION_ERROR` |
| 429 | `RATE_LIMITED` |
| 500 | `INTERNAL_ERROR` |
| 501 | `TOOL_NOT_ENABLED` |
| 502 | `MODEL_UNAVAILABLE`、`MODEL_EMPTY_RESPONSE`、`MEDIA_UPSTREAM_ERROR` |
| 503 | `MEDIA_BACKEND_MISSING`、`SESSION_EXPIRED` |
| 504 | `MODEL_TIMEOUT`、`MEDIA_UPSTREAM_TIMEOUT` |

行程导出另有 `PDF_FAILED`、图片代理有 `UPSTREAM_ERROR`，未进映射表，按 500 返回。错误码统一在 `api/client.ts` 归一化成中文，组件层不直接判断 code。

---

## 6. 国产安卓浏览器兼容（硬要求）

目标入口是华为/小米/vivo/OPPO 自带浏览器、UC/QQ、微信内置浏览器：

- SSE 必须做特性探测，`fetch + ReadableStream` 不可用时回退短轮询
- 前端无构建流程，不依赖 Vite/Babel 降级：新语法要自行规避（不用可选链、`??`、顶层 await 等过新特性）
- `AbortController` / `TextDecoder` / `fetch` 缺失要有降级或 polyfill
- `backdrop-filter`、`aspect-ratio`、flex `gap` 用 `@supports` 降级；不用 `100dvh`
- 视口 `viewport-fit=cover` + 安全区变量；软键盘弹起时输入区不能被遮住
- 图片 `onerror` 要有占位，省流模式不得导致卡片塌陷

---

## 7. 运行时语义（容易踩坑）

**会话**：单 key `chat:session:{id}` 存整段 JSON；`GET` 读全量、`SET` 写全量；`SESSION_TTL_SEC=0` 永不过期，非 0 时每次写入滑动续期（只读不续期）。同一会话并发写为后写覆盖。

**上下文**：保留 20 轮 + 滚动摘要；摘要失败不推进折叠计数，宁可多占上下文也不丢记忆。

**长期偏好**：按设备隔离（`X-Device-ID`），存 Redis 的 `user:profile:{id}`；回答完成后按信号词触发后台抽取，结果先存 `user:suggest:{id}`（1 小时过期）等确认。注入时拼在系统提示词末尾，最多 12 条 / 600 字，并注明「本次要求优先于长期偏好」。删除或清空后下一轮立即不再注入。

**SSE 流**：`StreamState` 只在后端进程内存，支持断线续传与取消；进程重启后无法续传，但已完成的回答已落库。内存里最多保留 200 条流，超出淘汰最旧的。

**权限**：工具通过 ContextVar 拿 `PermissionBroker`；无 broker（脱离会话直接调工具）一律拒绝；所有异常路径都收敛为「不执行」。

**媒体**：提交只返回 `task_id`，不阻塞聊天；前端轮询。图片本地落在 `media/task-<id前8位>.*`，Redis 记录任务元数据（TTL 同 `MEDIA_TASK_TTL_SEC`）；消息里的 `media_task_ids` 让刷新后能重建图片卡片。

**模型**：单次 `max_tokens=4096`，请求超时 300 秒，`max_retries=0`；agent 单回答最多 12 步；方舟/DeepSeek 必须关闭思考模式，否则正文会被 reasoning 吃光。

**并发**：无全局限流。瓶颈是单进程 + 上游配额；浏览器同域最多 6 个并发连接，多开标签页会排队。

**离线批处理**：`scripts/batch_chat.py` 用于成批跑提示词，不经过 agent（无工具、无流式）。

```powershell
.\.venv\Scripts\python.exe scripts\batch_chat.py 输入.jsonl 输出.jsonl --concurrency 4
```

- 输入：每行 `{"id":"q1","prompt":"帮我规划大理三日游"}`；输出逐行追加 `id/prompt/answer/usage`，失败带 `error`
- `--concurrency N` 就是同时在飞的模型请求数
- `--tier auto|flex|fast`；当前模型不支持 `flex`（会返回 400），默认 `auto`

---

## 8. 工程结构与代码位置

```
app/
  main.py                 FastAPI 入口、lifespan 装配、CORS
  api/routes/             chat / tools / permissions / media / system / trips / share
  schemas/                请求响应模型（字段名即契约）
  services/chat.py        会话 + agent 循环 → SSE；工具 category 映射
  services/streams.py     StreamState 事件缓冲与续传
  services/trips*.py      地图缓存、PDF、ICS、分享页
  agent/graph.py          LangGraph 图与 SYSTEM_PROMPT（MAX_STEPS=12）
  agent/model.py          模型接入与 tool calling 探测
  agent/permissions.py    权限 Broker（120 秒超时、默认拒绝）
  agent/tools/            工具注册表，按 info/plan/system/search/plugin/media 分组
  media/                  后端抽象、生图/生视频实现、任务存储、模板
  storage/                SessionStore 抽象 + Redis / 内存实现；profile.py 存长期偏好
  services/profile.py     偏好抽取、合并、注入系统提示词
  core/                   config / errors / context / security / net
web/                      前端：index.html / app.js / styles.css / serve.py / manifest / assets
scripts/batch_chat.py     离线批量调用模型（JSONL 进、JSONL 出）
```

前端是无构建静态页，改完刷新即可生效，不需要 npm 安装或打包。约定：所有网络请求集中在 `app.js` 的一个请求封装里，错误码在此统一转中文；不要在前端写任何密钥。

---

## 9. 联调自检（6 步）

1. `GET /system/capabilities` 首屏成功，入口按返回字段显示
2. 新建会话并发出第一条消息
3. 看到 token 逐段出现
4. 问天气出现 `get_weather` 工具卡片
5. 让它写文件到沙箱，弹出权限框
6. 提交生图，看到 pending → succeeded，图片经 `/tasks/{id}/image` 展示

```powershell
curl.exe http://localhost:8000/api/v1/health
curl.exe http://localhost:8000/api/v1/system/capabilities
curl.exe http://localhost:8000/api/v1/tools
```
