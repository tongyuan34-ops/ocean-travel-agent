# 前端分阶段生成提示词（AI Studio 用）

> 用法：**从上到下逐段粘贴**，一次只贴一个阶段。每段开头自带上下文，
> 即使新开对话也能独立执行。每阶段验收通过后再贴下一段。
>
> 如果中途被截断：把「已完成文件清单」贴回去，说「从阶段 N 继续，已有文件不要重写」。

---

## 全局约定（每段都隐含，不必重复粘贴）

- 技术栈：React 18 + TypeScript strict + Vite，**不用 UI 组件库**，手写 CSS
- 前端端口 **3001**，Vite proxy：`/api` → `http://localhost:8000`
- 构建目标 **ES2017**，兼容国产安卓自带浏览器
- 后端接口前缀 `/api/v1`，契约见随附文档，**不得修改后端**
- 所有注释与界面文案使用中文
- 禁止 `any`；禁止硬编码平台清单；禁止在前端碰 `.env` / cookie / token

### 设计令牌（所有阶段共用，阶段 0 会落地成 CSS 变量）

视觉基准来自用户提供的参考站（OCEAN）：**全屏海面日落视频 + 左对齐衬线大标题 + 胶囊按钮**。
首屏是亮的（天空/海面），主应用区是深的（深海），两者用一段渐变衔接。

| 令牌 | 值 | 用途 |
|---|---|---|
| `--sky` | `#cfdfee` | 首屏天空底色（视频加载前的兜底） |
| `--sun` | `#f3c99a` | 日落暖光点缀 |
| `--navy` | `#12283f` | 首屏主文字色（深于天空，保证对比度） |
| `--ink` | `#1c3a52` | 首屏次级文字 |
| `--sea` | `#2e7d8f` | 海水青，强调色基准 |
| `--deep` | `#0b2233` | 主应用区背景（深海） |
| `--foam` | `#eaf6f8` | 深色区文字 |
| `--sand` | `#e8dcc8` | 暖色点缀 |
| `--accent` | `#1f6f86` | 强调色（可被用户改） |
| 标题字 | `"Noto Serif SC", "Songti SC", serif` | 衬线，安静高级 |
| 正文字 | `"PingFang SC", "Microsoft YaHei", system-ui` | 无衬线，清晰 |
| 圆角 | 2px / 999px 两档 | 极简，不用 8px 中间值 |
| 动效 | `cubic-bezier(.22,.61,.36,1)`，280ms / 640ms 两档 | 克制、有质感 |
| 字距 | 大标题 `-0.02em`，英文小标签 `0.22em` | 参考站的排版特征 |

---

## 阶段 0：工程骨架与设计系统

```
你是一名资深前端工程师 + 视觉设计师。请为一个「旅行规划助手」搭建前端工程骨架。

技术栈硬要求：
- React 18 + TypeScript（strict）+ Vite，不使用任何 UI 组件库，样式手写 CSS
- 开发端口 3001；vite.config.ts 配置 proxy：'/api' -> 'http://localhost:8000'
- build.target = 'es2017'
- 目录：src/api、src/hooks、src/components、src/styles、src/theme

本阶段只做骨架和设计系统，不要写业务逻辑：
1. package.json（react、react-dom、vite、typescript、@vitejs/plugin-react、react-markdown、remark-gfm）
2. vite.config.ts、tsconfig.json（strict: true）
3. src/styles/tokens.css：定义全局 CSS 变量（颜色、字号阶、间距阶、圆角、缓动、阴影），
   深色为默认主题。文字色必须满足 WCAG AA 对比度。
4. src/styles/base.css：reset、字体栈、滚动条样式、选中色、focus-visible 描边
5. src/theme/ThemeProvider.tsx：用 Context 提供 { accent, layout } 两个可调项，
   写入 <html data-accent="..."> 与 <html data-layout="...">，并持久化到 localStorage。
   本阶段只需提供接口和默认值，设置面板留到阶段 6。
6. src/App.tsx：临时只渲染一个全屏占位，中央显示产品名「潮汐 · 旅行规划」
7. index.html：viewport 含 viewport-fit=cover；<html lang="zh-CN">；预置主题色 meta
8. public/assets/hero/ 目录占位：留出 hero.mp4 / hero.webm / poster.jpg 三个文件名，
   先用一张纯色占位图 poster.jpg，视频文件由使用者后续替换（不要用外链视频）

验收：
- npm i && npm run dev 在 3001 启动
- 控制台无 TS 报错、无 any
- 页面为深色，字体中文正常，无白屏闪烁

输出要求：逐个文件给出完整内容，不要省略、不要写「其余不变」。
```

---

## 阶段 1：静态视频首屏（不复刻滚动，直接播 AI 生成的海洋视频）

```
接上面已完成的工程骨架。现在实现首屏。

重要前提：首屏**不做任何滚动驱动动画、不做下潜交互**。
背景就是一段 AI 生成的海洋视频，自动静音循环播放；用户滚动只是正常离开首屏。

布局（严格按此，参考 OCEAN 风格，左对齐而非居中）：
- 全屏 100vh 背景视频，object-fit: cover，绝对定位铺满，z-index 0
- 视频之上叠加一层极轻的 scrim：linear-gradient(90deg, rgba(255,255,255,.42) 0%, rgba(255,255,255,.12) 46%, transparent 68%)，
  保证左侧深色文字在任何画面帧上都可读
- 顶部导航 72px：左侧文字标 + 手绘感波浪 SVG，中间三个极简导航项（发现 / 旅程 / 手记），
  右侧一枚描边胶囊按钮「开始规划 ↗」；导航无背景色，靠 scrim 保证可读
- 主内容左对齐，垂直居中偏上，最大宽度 640px，左内边距 clamp(24px, 7vw, 120px)：
  1) 小标签：FEEL THE WORLD SLOW DOWN，11px，letter-spacing .22em，全大写
  2) 主标题：衬线，两行，clamp(2.8rem, 6.6vw, 5.6rem)，字重 400，行高 1.02，字距 -0.02em
     文案：「慢一点，/ 看见海。」（中文用衬线，副标题可保留一行英文）
  3) 副文案：两行，16px，行高 1.7，颜色 --ink，最大宽度 34ch
     文案：「片刻的安静。/ 无限的可能。」
  4) 按钮组：实心深色胶囊「开始规划 →」+ 幽灵按钮「看一段海」（点击可全屏播放同一视频）
- 底部左侧：极小的滚动提示「向下浏览 ↓」，静态，不做动画；右下角一枚播放/暂停小按钮

视频实现（关键）：
- <video autoPlay muted loop playsInline preload="metadata" poster="/assets/hero/poster.jpg">
- 必须同时提供 MP4(H.264) 与 WebM(VP9) 两个 source，先 WebM 后 MP4
- 视频放在 public/assets/hero/ 下，命名 hero.mp4 / hero.webm / poster.jpg
- 不支持自动播放时（回退），显示 poster 静态图，不报错、不弹提示
- 用户开启 prefers-reduced-motion 时：不自动播放，仅显示 poster
- navigator.connection.saveData 为 true，或屏幕宽度 < 480px 且网络为 2g/3g 时：只加载 poster
- 首屏 unmount 或页面 hidden 时暂停视频，避免后台耗电
- 视频必须是「海面日落在水面」的明亮画面，**不要水下镜头**，否则深色文字读不清

响应式：
- ≥1024px：如上述左对齐构图
- 768–1023px：标题降到 3.2rem，scrim 加深到 .5
- <768px：导航只保留 logo 与「开始规划」；标题 2.4rem；
  按钮组纵向排列；视频改为 poster 优先（省流）
- 安全区：底部提示加 env(safe-area-inset-bottom)

视觉红线：
- 不要发光霓虹、不要 3D、不要粒子、不要毛玻璃导航
- 不要给视频加色偏滤镜，不要叠加蓝色蒙版改变原片色调
- 文字只用 navy / ink 两个层级，不超过两种字号强调

验收：
- 首屏在 1920、1366、375 三种宽度下构图都不塌
- 弱网只出 poster，不出现空白
- 滚出首屏后视频停止播放
- 控制台无 autoplay 相关报错

输出：OceanHero.tsx、OceanHero.css、HeroNav.tsx（如需拆分），完整内容。
```

### 阶段 1 附：视频素材怎么来

不用自己写 CSS 海洋。让 AI 生成一段 8–15 秒、可无缝循环的海面视频，要求：

```
生成一段 10 秒、可无缝循环的实拍感海洋空镜：
低角度贴水面，金色日落逆光，右侧海平线有太阳，天空有少量薄云，
水面持续起伏并有细碎高光；镜头极缓慢向前推进后回位，便于循环；
真实摄影质感，胶片颗粒轻微，色调参考 #cfdfee / #f3c99a / #12283f；
不要出现人物、船只、文字、水印；不要水下镜头；不要镜头剧烈运动。
输出 16:9，1920×1080 即可，文件尽量小（目标 < 8MB）。
```

拿到素材后放：
```
public/assets/hero/hero.webm    （VP9，码率 1.6–2.2 Mbps，首选）
public/assets/hero/hero.mp4     （H.264 High，码率 2.2–2.8 Mbps，兜底）
public/assets/hero/poster.jpg   （取视频第 1 秒，宽 1920，质量 82）
```

---

## 阶段 2：应用外壳与聊天布局

```
沿用已有工程与首屏。现在搭建首屏之下的主应用外壳（不含流式逻辑）。

布局：
- 左侧会话栏（桌面 280px，可折叠；移动端变为抽屉）
- 中间对话区（最大宽度 780px，居中，顶部留白充足）
- 底部输入区（吸附底部，适配安全区 env(safe-area-inset-bottom)）
- 右侧可选信息区留空，阶段 5/6 使用

组件与职责：
1. Sidebar.tsx：新建会话、会话列表、当前项高亮、删除（二次确认）、折叠按钮
2. ChatWindow.tsx：消息列表容器 + 自动滚动策略（用户上滚时暂停并显示「回到底部」）
3. MessageBubble.tsx：用户消息右对齐、助手消息左对齐；助手消息支持 Markdown
   （react-markdown + remark-gfm，代码块带复制按钮，链接 target="_blank" rel="noopener"）
4. InputBox.tsx：自适应高度 textarea；Enter 发送、Shift+Enter 换行；
   发送中禁用并显示「正在思考…」；有停止生成按钮（先留回调）
5. AppShell.css：所有布局与响应式断点（≤768px、≤420px）

数据接入：
- src/api/client.ts：统一 fetch 封装，解析 { code, message }，导出 ApiError 类
- src/api/types.ts：按契约定义 SessionCreated、MessageItem、MessageList、ToolItem、
  Capabilities、TaskAccepted、TaskStatusResponse、ImageTemplate
- src/api/chat.ts：createSession / deleteSession / listMessages（本阶段不写 SSE）
- src/api/system.ts：getCapabilities / getHealth
- src/hooks/useCapabilities.ts：首次加载一次，失败可重试；暴露 loading/error

要求：
- 字段名必须与契约完全一致，不得改写
- 能力未加载完成时，相关入口处于禁用态
- 空状态要有设计感：一句短文案 + 三个示例提问（可点击填入输入框）

验收：
- 新建会话能拿到 session_id 并出现在侧栏
- 切换会话会调用历史接口
- 窄屏下侧栏变抽屉，输入框不被遮挡

输出所有新增/修改文件的完整内容。
```

---

## 阶段 3：SSE 流式对话（含降级）

```
现在接入 POST /api/v1/chat/sessions/{id}/messages 的 SSE 流。

协议（事件均为 data: {json}\n\n）：
{"type":"token","content":"..."}
{"type":"tool_call","call_id":"...","tool":"get_weather","category":"info","args":{...}}
{"type":"tool_result","call_id":"...","status":"ok|error","summary":"..."}
{"type":"permission_request","request_id":"...","action":"write_file","detail":{...}}
{"type":"end","message_id":"...","finish_reason":"stop|length"}
{"type":"error","code":"MODEL_TIMEOUT","message":"..."}

实现要求：
1. src/api/sse.ts：fetch + ReadableStream + TextDecoder 手工解析
   - 缓冲区按行切分，遇空行视为事件结束，必须正确处理半包/粘包
   - 单条 JSON 解析失败只记 warning，绝不中断整条流
   - 支持 AbortController 中断
2. 降级：探测 fetch / ReadableStream / TextDecoder / AbortController
   - 任一缺失时，切换到 src/api/polling.ts 的短轮询实现
   - 轮询接口同样返回上述事件数组，保持上层 Hook 接口一致（不要两套 UI 逻辑）
3. src/hooks/useChat.ts：
   - 状态：idle | streaming | awaiting_permission | stopped | error
   - 累加 token 到当前助手消息；tool_call/tool_result 暂存到 events 数组（阶段 4 渲染）
   - end 时若 finish_reason === 'length'，在消息尾部追加提示「回答已达长度上限」
   - error 时把 code 映射成中文文案，展示可重试按钮
4. 打字机效果：按 token 追加即可，不要人为 setTimeout 拖慢
5. 多轮：发送新消息前不得自行拼接历史，历史由后端管理

验收：
- 真实后端下能逐字出现
- 中途点「停止」立即断开且不报错
- 手动模拟半包（把一个事件拆成两次 read）仍能正确解析

输出 sse.ts、polling.ts、useChat.ts 及 ChatWindow 的必要改动，全部完整内容。
```

---

## 阶段 4：工具卡片与来源呈现

```
现在把 SSE 里的 tool_call / tool_result 渲染成「agent 工作轨迹」。

组件：
1. ToolCallCard.tsx
   - 收到 tool_call 立即插入，状态「执行中」；同 call_id 的 tool_result 更新为
     「已完成」/「失败」，展示 summary（截断 200 字，可展开全文）
   - 标题显示中文名：从 GET /api/v1/tools 的 description 取；取不到时用工具名兜底
   - 参数默认折叠，点击展开为键值列表
   - 视觉：左侧 2px 竖直强调线 + 极浅底色；与助手正文明确区分
   - status=error 时整卡转为低饱和红棕，summary 作为失败原因
2. ToolTrace.tsx：一轮对话内多个工具调用按时间顺序纵向排列，
   search/plugin 类 summary 首行是统计（命中源数/各源命中数/耗时/是否降级），
   把首行做成独立的小标签行，剩余内容折叠
3. SourceList.tsx：从工具 summary 文本中提取 URL 渲染为可点击外链
   （只做 URL 提取，不要解析点赞数等数字，后端没有结构化字段）

必须遵守：
- 不得把 tool_call 的 args 直接展示原始 JSON，要做成中文键名映射表
- 工具卡片默认不抢视觉焦点：比正文低一个亮度层级
- 移动端卡片内文字 13px，行高 1.6，长 URL 自动换行不撑破容器

验收：
- 问「三亚最近天气」能看到 get_weather 卡片从执行中变为已完成
- 失败工具卡片显示红色状态与原因
- 一轮内 3 个工具调用顺序正确

输出全部文件完整内容。
```

---

## 阶段 5：权限确认与媒体生成

```
现在实现两个高交互模块。

A. 权限确认（PermissionDialog.tsx）
- 收到 permission_request 立即阻断式弹窗，不可点击遮罩关闭
- 展示 action 的中文映射（write_file 写文件 / create_dir 建目录 /
  delete_path 删除 / set_wallpaper 切换壁纸）与 detail.path、detail.preview
- 三个按钮：拒绝 / 允许本次 / 允许且本会话不再询问
- 点击调用 POST /api/v1/permissions/{request_id}/decision，body { granted, remember }
- 120 秒倒计时，超时自动关闭并提示「已自动拒绝」
- 绝不在任何位置展示 cookie / token

B. 媒体生成（MediaPanel.tsx + src/api/media.ts）
- GET /api/v1/media/templates 渲染模板选择器（6 个：打卡/海报/手账/美食/夜景/人像）
  每个模板卡片显示 label 与一句话场景描述，选中后用 key 作为 style 参数
- POST /api/v1/media/images 提交 → 立即插入 pending 占位卡片
- GET /api/v1/media/tasks/{id} 轮询：2s 起，退避到 5s 上限，succeeded/failed 停止
- 成功：展示图片 + 「另存为」（a[download]）+「重新生成」
- 失败：展示 error 中文文案；MEDIA_BACKEND_MISSING 时提示「未配置生图后端」
- 必须固定提示：图片链接 24 小时内失效，请及时保存
- 若 capabilities.image_gen 为 false，整个入口不渲染（不是禁用）

验收：
- 写文件请求会弹窗，拒绝后对话继续且工具显示失败
- 提交生图能看到 pending → succeeded 的完整状态流转
- 图片加载失败时卡片不塌陷，有占位

输出全部文件完整内容。
```

---

## 阶段 6：个性化（壁纸 + 主题色 + 排版风格）

```
现在做个性化系统，这是本产品的差异化重点。全部为纯前端能力，不调用后端。

1. 壁纸（WallpaperLayer.tsx + useWallpaper.ts）
   - 用户可从本地上传图片：FileReader → 压缩到最长边 2560px、质量 0.82 的 WebP/JPEG
     → 存入 IndexedDB（不要 localStorage，base64 会超配额）
   - 提供 4 张内置海洋渐变壁纸（纯 CSS 生成，不依赖外链图片）
   - 壁纸层固定在全屏最底层，其上叠加自适应遮罩：
     根据图片亮度自动选择 rgba(0,0,0,.18~.55) 的 scrim，保证正文可读（对比度 ≥ 4.5:1）
   - 支持模糊度（0–20px）、暗化强度（0–60%）两个滑杆
   - 壁纸只在首屏之下的应用区生效；首屏固定播放海洋视频，不参与壁纸替换

2. 主题色（AccentPicker.tsx）
   - 6 个预设色 + 自定义取色器（input[type=color]）
   - 只改变强调色，其他色由 HSL 推导：hover / active / 10% 透明底 / 30% 描边
   - 自动校验与背景的对比度，不达标时自动提高亮度并提示
   - 写入 <html data-accent>，通过 CSS 变量生效，不做运行时 JS 换肤

3. 排版风格（LayoutPicker.tsx）
   - 三套：极简留白（大字距、宽行距、无边框）/ 杂志（衬线标题、编号章节）/
     沉浸暗色（低对比、窄栏、弱化分隔线）
   - 通过 <html data-layout> 切换，只改 CSS 变量与少量布局规则
   - 切换要有 280ms 过渡，且不引起布局跳动（预留空间）

4. 设置面板（SettingsPanel.tsx）
   - 右侧抽屉，分「外观」「壁纸」「排版」三组
   - 全部即时预览，提供「恢复默认」
   - 持久化到 localStorage（壁纸文件除外，走 IndexedDB）
   - 首屏顶部的主题按钮打开此面板

注意区分：这里改的是「网页外观」。后端 capabilities.wallpaper 指的是
「把图片设置为用户电脑桌面壁纸」，属于 system 组工具，需要权限弹窗；
两者不要混淆，前端不得把两者做成同一个按钮。

验收：
- 上传壁纸后刷新仍保留
- 切换任意主题色与排版，正文对比度仍达标
- 低端安卓机上切换不掉帧（避免大面积 blur 重绘）

输出全部文件完整内容。
```

---

## 阶段 7：兼容性、性能与收尾自检

```
最后一阶段：按国产安卓自带浏览器（华为/小米/vivo/OPPO、UC、QQ 浏览器、
微信/QQ 内置）做兼容与打磨，并给出自检清单。

1. 特性探测与降级
   - fetch / ReadableStream / TextDecoder / AbortController / IntersectionObserver
     全部做能力探测，缺失走降级路径（SSE → 短轮询；IObserver → 滚动监听）
   - CSS 用 @supports 包裹 backdrop-filter、aspect-ratio、gap；
     不支持时回退为实色 / padding hack / margin
   - 不用 100dvh，改用 height:100% + JS 读取 window.innerHeight 修正

2. 移动端
   - viewport-fit=cover + env(safe-area-inset-*)
   - 软键盘弹起时输入区不被遮挡：监听 visualViewport.resize 调整容器高度
   - 点击区域 ≥ 44px；禁用双击缩放带来的 300ms 延迟（touch-action: manipulation）
   - 首屏视频在低端机降级：只加载 poster，不加载 webm/mp4

3. 性能
   - 首屏关键 CSS 内联；图片 lazy loading；字体 font-display: swap
   - 长对话列表做窗口化或分页（超过 200 条时）
   - 滚动与动画只用 transform/opacity，禁止动画 width/height/top

4. 可访问性
   - 所有交互元素可键盘到达，focus-visible 清晰
   - 弹窗聚焦陷阱 + Esc 关闭（权限弹窗除外）
   - 图片有 alt，按钮有 aria-label

5. 收尾
   - 统一错误码到 src/api/errors.ts 的中文文案表，组件层不判断 code
   - 删除所有 console.log，保留必要 warn
   - 输出一份 README.md：启动命令、目录说明、已知限制

验收清单（逐条确认并在回答中打勾）：
□ npm run build 通过，无 TS 错误
□ 375×667 下无横向滚动
□ 关闭动画偏好后首屏仍完整
□ 断网时报错清晰且可重试
□ 权限弹窗不遮挡且不被背景滚动穿透
□ 主题色与壁纸刷新后保持
□ 开启「省流模式」页面不塌陷

输出改动文件完整内容 + README.md。
```

---

## 附：如何判断 AI 有没有跑偏

出现以下情况，直接让它重做该阶段，不要在其基础上继续：

| 症状 | 说明 |
|---|---|
| 首屏出现发光霓虹、3D 地球、粒子爆炸 | 审美跑偏，违反"简约大气" |
| 用 canvas / three.js 做海洋 | 首屏只放视频，不需要任何 canvas |
| 首屏做成滚动下潜动画 | 已明确不做交互，只播视频 |
| 视频用外链地址（如 Pexels/CDN） | 必须放 public/assets/hero/ 本地文件 |
| 引入 Ant Design / MUI / Tailwind 组件库 | 违反硬要求 |
| 一次输出超过 600 行且开始省略文件 | 必然被截断，改要求逐文件输出 |
| 把 `capabilities.platforms` 写死 | 违反契约 |
| 展示实时酒店价格 / 班次票价 | 后端没有这些数据，属于编造 |
| 把「网页壁纸」和「设置电脑桌面壁纸」混为一个功能 | 概念错误 |

## 附：每阶段完成后要保存的文件

```
阶段0  package.json  vite.config.ts  tsconfig.json  index.html
       src/styles/tokens.css  src/styles/base.css  src/theme/ThemeProvider.tsx  src/App.tsx
阶段1  src/components/ocean/OceanHero.tsx  OceanHero.css  HeroNav.tsx
       public/assets/hero/{hero.webm, hero.mp4, poster.jpg}
阶段2  src/components/Sidebar.tsx  ChatWindow.tsx  MessageBubble.tsx  InputBox.tsx
       src/api/{client,types,chat,system}.ts  src/hooks/useCapabilities.ts
阶段3  src/api/{sse,polling}.ts  src/hooks/useChat.ts
阶段4  src/components/{ToolCallCard,ToolTrace,SourceList}.tsx
阶段5  src/components/{PermissionDialog,MediaPanel}.tsx  src/api/media.ts
阶段6  src/components/{WallpaperLayer,AccentPicker,LayoutPicker,SettingsPanel}.tsx
       src/hooks/{useWallpaper,useTheme}.ts
阶段7  src/api/errors.ts  README.md
```
