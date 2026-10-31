# 部署与配置手册（SETUP）

> 本文档是**操作步骤**，与 `SPEC.md`（接口契约与实现要求）互补。
> SPEC 是"要建成什么样"，SETUP 是"怎么把它跑起来"。

## 链路总览

    图片：旅行助手后端(8000) → Infinite Canvas(8080) → 火山方舟 Seedream
    视频：旅行助手后端(8000) ────────────────────────→ 火山方舟 Seedance（直连）

上游为**合规商业渠道**：火山方舟的即梦 Seedream（图片）与 Seedance（视频）。
**图片**经 Infinite Canvas 中转，**视频建议直连**（原因见第 4 步）。

| 组件 | 角色 | 端口 |
|---|---|---|
| 旅行助手后端 | 我们的 FastAPI 服务 | 8000 |
| 旅行助手前端 | React + Vite | 3001 |
| Infinite Canvas 后端 | 图片代理 + 工作台 API | 8080 |
| Infinite Canvas 前端 | Next.js 画布界面 | 3000 |
| 火山方舟 | 云端官方生图/生视频 | — |

---

## 第 1 步 · 开通火山方舟并拿凭据

1. 注册/登录火山引擎，进入方舟控制台：https://ark.volcengine.com/
2. 创建 API Key：https://ark.volcengine.com/region:cn-beijing/apikey
3. 在模型广场**开通**要用的模型（开通后才能调用）：
   - 图片：`doubao-seedream-4-0-20260415`（4.0，性价比）
     或 `doubao-seedream-5-0-pro-260628`（5.0 pro，效果更好）
   - 视频：`doubao-seedance-2-5-260628`（或按需选 2.0 系列）
4. 记下 `ARK_API_KEY`，形如 `ark-xxxx`。

**费用参考**（以官网实价为准）：Seedream 4.0 文生图约 0.2 元/张；视频按秒计费。
建议先在控制台设好预算上限。

## 第 2 步 · 部署 Infinite Canvas

用你给的那份安装包（Windows）：`Infinite.Canvas_0.8.1_x64-setup.exe`

或从源码起（Docker）：

    git clone https://github.com/tigerowo/infinite-canvas.git
    cd infinite-canvas
    copy .env.example .env
    docker compose up -d --build

`.env` 至少改这几项：

    ADMIN_USERNAME=admin
    ADMIN_PASSWORD=<改成你自己的密码>
    JWT_SECRET=<改成随机的长字符串>      # 默认值是 infinite-canvas，正式部署必须改
    JWT_EXPIRE_HOURS=168

跑起来后访问 http://localhost:3000（前端），后端在 http://localhost:8080。

## 第 3 步 · 在 Infinite Canvas 里配置火山方舟渠道（图片）

进入后台，在**系统设置 → 渠道（channels）**新增：

    protocol : openai
    name     : 火山方舟
    baseUrl  : https://ark.cn-beijing.volces.com/api/v3
    apiKey   : <ARK_API_KEY>
    models   : ["doubao-seedream-4-0-20260415"]
    enabled  : true
    weight   : 1

并把公开配置里的 `defaultImageModel` 设为 `doubao-seedream-4-0-20260415`。

验证：打开**生图工作台**生成一张图。出图即表示图片链路已通。

## 第 4 步 · 视频生成怎么接（重点）

### 结论先说：视频**直连火山方舟**，不走 Infinite Canvas

原因：火山方舟的**图片**接口是 OpenAI 风格（`/v3/images/generations`），
Infinite Canvas 的 `openai` 协议能直接用；
但**视频**走的是 `/contents/generations/tasks` 这套异步任务制，
**不是 OpenAI 风格**，要让 Infinite Canvas 转接，只能靠它的
「渠道自定传参转译」（`parameterTranslation`）——而该字段的原文格式
**没有公开文档**，得读源码或实测，维护成本高且易随版本失效。

所以：**程序化生成视频，我们的后端直接调火山方舟**；
Infinite Canvas 保留作你手动创作的画布与工作台。

### 火山方舟视频接口（已核准）

**Step 1 · 创建任务**

    POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
    Authorization: Bearer <ARK_API_KEY>
    Content-Type: application/json

    {
      "model": "doubao-seedance-2-5-260628",
      "content": [
        { "type": "text", "text": "女孩抱着狐狸，镜头缓缓拉出，头发被风吹动" },
        { "type": "image_url", "image_url": { "url": "https://.../first_frame.png" } }
      ],
      "generate_audio": true,
      "ratio": "adaptive",
      "duration": 5,
      "watermark": false
    }

响应：`{ "id": "<TASK_ID>" }`

**Step 2 · 查询任务**

    GET https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/<TASK_ID>
    Authorization: Bearer <ARK_API_KEY>

响应：

    {
      "id": "<TASK_ID>",
      "model": "doubao-seedance-2-5-260628",
      "status": "succeeded",                  // queued / running / succeeded / failed
      "content": { "video_url": "<VIDEO_URL>" },   // 结果 MP4 下载地址
      "usage": { "total_tokens": 246840 },
      "created_at": 1765510475,
      "updated_at": 1765510559
    }

- 异步过程：创建拿 ID → 轮询到 `succeeded` → 从 `content.video_url` 取 MP4
- 也支持 **Webhook** 接收状态变化，可替代轮询（任务量大时用）
- 只支持查询最近 7 天的任务记录

### 与 SPEC 契约的映射（照这个实现）

| SPEC 契约 | 火山方舟 | 说明 |
|---|---|---|
| `POST /api/v1/media/videos` 的 `prompt` | `content[0].text` | 提示词 |
| `ref_image`（参考图） | `content[]` 中的 `image_url` 项 | 图生视频/首帧 |
| `duration`（秒） | `duration` | 直接透传 |
| 返回 `202 {task_id, status: pending}` | `POST .../tasks` 返回的 `id` | **内部 id 与 Ark task id 需做映射表** |
| `GET /api/v1/media/tasks/{id}` 的 `status` | 查询接口的 `status` | 见下方映射 |
| `result_url` | `content.video_url` | 结果地址 |
| 失败时的 `error` | 查询接口的错误字段 | 原样透传，不要吞掉 |

状态映射：

    Ark queued    → pending
    Ark running   → running
    Ark succeeded → succeeded（result_url = content.video_url）
    Ark failed    → failed（error = 上游原因）

**实现要点**：
- 我们的 `task_id` 用内部 uuid，另存一张 `our_id → ark_id` 映射表；
  前端只看到我们的 id，不暴露 Ark 的
- 轮询不要打太密：建议 3 秒起、退避到 10 秒上限；用 `updated_at` 判断是否有变化
- 视频生成通常几十秒到几分钟，前端轮询策略参考 SPEC 第三部分 3.7

### 顺带一个很有用的组合玩法

火山方舟支持**图生视频**，而你的场景正好串得上：

    用户粘贴抖音打卡图文 → 解析要素 → Seedream 生成打卡图
      → 把这张图作为 image_url 传给 Seedance → 生成动态短视频

也就是说「先出图、再让图动起来」这条链路，不需要额外能力，用现有两个接口就能拼出来。

## 第 5 步 · 接入旅行助手后端

**图片**这一步需要先拿到 Infinite Canvas 的调用凭据。它没有对外 API 文档，
最可靠的办法是实测抓包：

1. 打开 Infinite Canvas 的生图工作台，生成一张图
2. 浏览器 F12 → Network 面板，找到发往 `/api/v1/images/generations` 的请求
3. 复制它的 **完整 URL、Authorization 头、请求体**

`.env` 配置：

    # 模型
    MODEL_PROVIDER=openai_compatible
    # deepseek-flash = DeepSeek-V4.1-Flash：1M 上下文，支持 tool calls 与视觉
    MODEL_NAME=deepseek-flash
    MODEL_API_KEY=<DeepSeek Key>
    MODEL_BASE_URL=https://api.deepseek.com

    # 图片：经 Infinite Canvas
    IMAGE_BACKEND=custom
    CUSTOM_IMAGE_ENDPOINT=http://127.0.0.1:8080/api/v1/images/generations
    CUSTOM_IMAGE_TOKEN=<抓到的 Authorization 值>

    # 视频：直连火山方舟
    VIDEO_BACKEND=ark
    ARK_API_KEY=<ARK_API_KEY>
    ARK_VIDEO_MODEL=doubao-seedance-2-5-260628
    ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3

**注意两个坑**：

1. **响应结构不同**：Infinite Canvas 统一返回 `{code, data, msg}`，
   `code=0` 才算成功，**业务失败也返回 HTTP 200**——适配层必须判 `code`，
   不能只看 HTTP 状态码。火山方舟则是标准的 HTTP 语义 + JSON。
2. 视频是异步任务，与 SPEC 里 media 组的模型一致，按第 4 步的映射表对接。

## 第 6 步 · 另外三个 Key（与 Infinite Canvas 无关，独立申请）

| 变量 | 用途 | 申请入口 |
|---|---|---|
| 天气 API Key | get_weather | 和风天气 或 OpenWeatherMap |
| 地图 API Key | plan_route / search_poi | 高德开放平台 Web 服务 API，或百度地图开放平台 |
| BING_SEARCH_API_KEY | search_web | Bing Search API（Azure）；不填则回退 HTML 抓取 cn.bing.com |

三者都填进旅行助手后端的 `.env`。未配置的工具会返回 `TOOL_NOT_ENABLED`，
并在 `/api/v1/system/capabilities` 中标记为不可用。

---

## 验证清单

| 检查项 | 命令 / 方式 |
|---|---|
| 火山方舟 Key 有效 | 控制台调试台试调一次图片或视频 |
| Infinite Canvas 前端 | 浏览器打开 http://localhost:3000 |
| Infinite Canvas 后端 | 浏览器打开 http://localhost:8080 |
| 图片链路通 | Infinite Canvas 生图工作台出图 |
| 视频链路通 | 用 curl 按第 4 步创建一个任务，轮询到 succeeded |
| 我们后端活着 | `curl.exe http://localhost:8000/api/v1/health` |
| 能力开关正确 | `curl.exe http://localhost:8000/api/v1/system/capabilities` |

`capabilities` 中 `image_gen`、`video_gen` 应为 true，`platforms` 应出现预期来源。

---

## 风险与许可

### 火山方舟（上游）
- 官方商业 API，合规使用；**按量付费**，图片按张、视频按秒
- 生成的图片/视频请遵守其内容规范

### Infinite Canvas
- 许可：**AGPL-3.0**
- 作为**独立服务**通过 HTTP 调用，一般不影响你方项目的许可
- 但若把它的代码**并入你自己的仓库**、修改后对外提供服务，
  你的项目需承担 AGPL 的开源义务（含网络服务场景）
- 项目自述"处于开发阶段，不保证历史数据兼容"

### 备选上游
- 阿里云百炼（通义万相）、智谱 CogView 等同样是官方商业 API
- 换供应商**不需要改我们后端的适配层代码**，只改配置与 `.env` 即可
