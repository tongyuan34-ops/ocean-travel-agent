# 安全与健壮性审计报告

日期：2026-11-16　范围：`app/`（111 个文件 / 2.0 万行）、`web/serve.py`、部署配置
基线：890 个测试通过、`ruff` 无告警、`mypy` 111 文件无问题

> 结论先说：**代码内部的质量高于平均水平**（路径沙箱、SSRF、权限网关、错误码契约都做得扎实），
> 问题几乎全部集中在**部署边界**——代码假设「只有我自己在访问」，但这个假设从未被强制。

---

## 一、P0：可导致数据泄露或额度被盗用

### P0-1 对外服务缺密码时「只警告不拒绝」

**位置** `app/standalone.py:128`

**现象** `WEB_AUTH_PASS` 未配置时，只打一条 `logger.warning` 就继续提供服务，
而该入口按文档就是 `--host 0.0.0.0`。

**为什么严重** 一旦没有密码，同一网络内任何人可以：

| 攻击面 | 后果 |
|---|---|
| `GET /api/v1/sessions` | 列出**全部**会话（`ChatSession` 没有 user 字段，`MemorySessionStore.list()` 不过滤） |
| `DELETE /api/v1/sessions/{id}` | 删掉任意会话 |
| `GET /api/v1/sessions/{id}/messages` | 读完整历史（含行程、上传文档元数据） |
| 文件类工具 | 在白名单内读写文件 |
| `POST /api/v1/chat/...` | 烧光模型额度 |

警告在控制台里滚过去就没了，服务照常对外——**等于没有防线**。

**修复** 改为 fail-closed：没密码直接 `RuntimeError` 拒绝启动，报错里给出下一步动作；
确实要裸跑的场景（内网、前面有反代鉴权）显式设 `ALLOW_OPEN_ACCESS=1`。

### P0-2 docker compose 把 api 直接发布到宿主机

**位置** `docker-compose.yml`

**现象** `api` 服务原本是 `ports: - "8000:8000"`，而它跑的是 `app.main:app`——
**完全没有鉴权中间件**。给 `web` 服务设的 `WEB_AUTH_PASS` 一点忙都帮不上，
因为攻击者根本不用走 3001 端口。

**修复** `api` 改为 `expose: ["8000"]`（只在 compose 内部网络可达）；
`web` 的 `WEB_AUTH_PASS` 用 `${WEB_AUTH_PASS:?...}` 强制填写，缺了就在启动前报错。

### P0-3 `web/serve.py` 无密码时同样绑 0.0.0.0

**现象** 无论有没有密码都 `ThreadingHTTPServer(("0.0.0.0", port))`，
而 `read_auth()` 在未配置时返回空串 → 整个局域网可打开页面并代理到后端。

**修复** 绑定地址跟着「有没有密码」走：无密码拒绝启动；`--no-auth`（本机调试）只绑 `127.0.0.1`。

---

## 二、P1：会造成数据丢失或资源耗尽

### P1-1 同一会话并发 → 静默丢消息

**位置** `app/services/chat.py:start_reply`

**现象** 会话存储是「读整份 → 改 → 写回」，`ChatService` 又是进程内单例。
同一会话上两轮并发时，两边各自拿一份副本、各自 append、各自 save，
**后写的覆盖先写的**——用户看到「我刚发的那条不见了」，而日志里两轮都成功。

**触发** 连点发送、两个标签页同时发。

**修复** 新增 `_busy_sessions: dict[session_id, stream_id]` 闸门，重复发送返回
`RATE_LIMITED`（429）并说明原因。选择「拒绝」而非「排队」：同一会话的并发几乎
一定是误触，排队只会让用户等一份他不想要的结果。

**关键细节** 闸门在 `_run_reply` 的 `finally` 里**先放闸再关流**。
只在 `task.add_done_callback` 里放会留一个空隙：`finish()` 一关客户端立刻就能发下一条，
而回调要等这一帧事件循环跑完——用户看到「刚答完就发不出去」。

### P1-2 没有全局并发上限

**现象** 每一轮都是一次真实的模型调用，但没有任何地方限制同时在跑的轮数。
前端连点、脚本重试、多标签页同发都会叠加，额度没有上限地消耗。

**修复** 新增 `MAX_CONCURRENT_REPLIES`（默认 4），超限返回 429。

---

## 三、P2：功能缺陷与长期风险

| 编号 | 问题 | 处理 |
|---|---|---|
| P2-1 | `/share/{token}` 被 Basic Auth 挡住——分享页的凭据是 URL 里的 96 bit token，再要一次密码会让「把行程发给朋友」直接失效，而页面只弹一个登录框，用户想不到是密码问题 | **已修**：中间件放行 `/share/` 前缀 |
| P2-2 | 单租户假设未被强制：会话没有 user 维度，`X-Device-ID` 只隔离 profile 记忆。多设备共用一个密码 = 共享全部会话 | **已修**：`ChatSession.tenant` 建会话时盖章，列表按租户过滤，消息/用量/风格/删除/发消息越权一律 404；配置 `TENANT_TOKENS` 后身份由 `Authorization: Bearer` 决定（请求头不再可信），未配置时保持旧的 `X-Tenant-ID` 回落；并发配额按租户单独计算；附件取用校验归属 |
| P2-3 | 内置 key 是「混淆」不是加密：`scripts/make_bundled_keys.py` 生成的 `_bundled_keys.py` 可从 exe 还原 | **未改**：属产品决策。建议改为服务端代理，客户端不再持有 key |
| P2-4 | `data/sessions.json` 单文件全量重写、`SESSION_TTL_SEC=0` 永不过期 | **部分已修**：新增 `STORAGE_BACKEND=sqlite`（`app/storage/sqlite.py`），写入只碰一行、列表走 `tenant` 索引、启动不再全量解析；150 条实测 save 2.73ms → 0.23ms，建 500 条 1.39s → 0.13s。历史数据用 `scripts/migrate_sessions.py` 迁移；桌面版升级时若 `data/sessions.db` 不存在而 `data/sessions.json` 在，启动会自动导入，不需要用户动手。TTL 仍默认 0 |
| P2-5 | `使用说明.txt` 只在文件不存在时写一次，老用户永远看不到更新后的说明 | **已修**：内容不同才覆盖（相同则不动，避免修改时间一直变）。实测安装目录里那份是 1039 字节的旧版，当前版本生成 1423 字节 |

---

## 四、已核查、确认无问题的部分

审计不是只找茬，以下是我逐项验证过、确实做得对的地方：

- **路径沙箱**（`app/core/security.py`）：先 `resolve()` 再校验、按路径层级比较（不是字符串前缀，`/data-backup` 不会被 `/data` 放行）、白名单为空时一律拒绝。
- **SSRF**（`app/core/net.py`）：协议白名单 + 内网/元数据网段黑名单 + **不自动跟随跳转**（逐跳重新校验，挡住 302 跳 169.254.169.254）+ 响应体与超时上限。
- **权限网关**（`app/agent/permissions.py`）：默认拒绝、拿不到 broker 也拒绝、超时 120s 视为拒绝；跨进程用 `RedisDecisionChannel` 覆盖「裁决比订阅先到」的竞态。
- **上传**（`app/media/uploads.py`）：12MB / 32MB 双上限，`DecompressionBombError` 已捕获。
- **分享令牌**（`app/services/trip_share.py`）：`token_urlsafe(12)`（96 bit）+ 正则全匹配防路径穿越 + 过期即删文件。
- **错误响应**：`INTERNAL_ERROR` 只回「服务器内部错误」，不泄露异常详情。
- **密钥卫生**：`.env` / `config.json` / `grants.json` / `cookies/` / `_bundled_keys.py` 全部在 `.gitignore` 内，仓库里没有真实密钥。
- **依赖**：`uv.lock` + `requirements.txt`（带 hash）双锁，`pyproject` 用上下界而非浮动版本。

---

## 五、验证方式

```bash
# 全量回归（890 passed, 1 skipped）
.venv/Scripts/python.exe -m pytest -q

# 静态检查
.venv/Scripts/python.exe -m ruff check app tests scripts web
.venv/Scripts/python.exe -m mypy app

# 新增用例（本次修复的回归防线）
.venv/Scripts/python.exe -m pytest tests/test_standalone.py tests/test_concurrency_guard.py -q
```

手工验证 fail-closed：

```bash
# 应报错退出，并提示设置 WEB_AUTH_PASS
WEB_AUTH_PASS= .venv/Scripts/python.exe -m uvicorn app.standalone:app --host 0.0.0.0 --port 8000

# 显式放行才允许裸跑
WEB_AUTH_PASS= ALLOW_OPEN_ACCESS=1 .venv/Scripts/python.exe -m uvicorn app.standalone:app --port 8000
```

---

## 六、打包与安装验证（2026-11-21，v0.2.0）

| 项 | 结果 |
|---|---|
| 构建 | `python scripts/build_desktop.py --installer`，冒烟全绿（健康检查 / 文件日志 / 首次运行产物 / 首页 12401 字节 / 技能目录 ≥2 且运行时加载数 >0） |
| 安装包 | `dist/OceanTravelAgent-0.2.0-setup.exe`，42,691,437 B，sha256 前 16 位 `651041399895727b` |
| zip | `dist/OceanTravelAgent-0.2.0-win64.zip`，50,405,680 B，sha256 前 16 位 `fc945fc69bf5664a` |
| 三处一致 | `dist/` `releases/` `安装包/` 哈希相同 |
| 窗口 | 无边框原生窗口实测打开（标题 `Ocean · 旅行规划`，进程 responding）；打包机缺 pywebview 会静默退回浏览器，已在 `build_desktop.py` 加硬校验 |
| 存储 | 启动日志确认「会话存储：SQLite（data/sessions.db）」；旧 `sessions.json` 首启自动导入 |
| 接口 | 首页 200；`/health` 版本号 0.2.0、`skills` 为 4；`GET /api/v1/tools` 返回 34 个工具，含 `discover_tools`、`use_skill`（category=meta） |
| 技能 | 包内 `_internal/skills/` 4 个目录（budget-split / trip-check / destination-compare / packing-list），运行时加载 4 个 |
| 说明书 | `使用说明.txt` 版本号随 `app/version.py` 更新为 0.2.0 |
