# 项目复盘：踩过的坑、修法，以及 Redis / MCP 的实现解析

这份文档不是给 PR 描述用的，是给「半年后自己再看到这段代码」用的。

每条都按同一个骨架写：**症状 → 根因 → 修法 → 怎么验证**。
只写「改了什么」而没有验证方式的条目，等于没修——下次它换张脸回来，你还是认不出。

## 对话顺序索引

按「你当时怎么提的」排，不是按模块排——找东西的时候人记得的是自己说过什么。

| 你当时说的 | 对应条目 |
| --- | --- |
| 下载好点击，等好久才弹出页面 | A1 |
| 把「晨雾海湾」去掉，加这两张图 | A2 |
| 这个项目在旅游方面能做什么 | 不是问题，是能力清单（见 README 第 3 节） |
| 排完行程要能出 A4 PDF / 路线图，先问、做一个、再问另一个 | B1–B5 |
| 检索失败（`KNOWLEDGE_EMPTY`）+ 关窗口历史消息会消失吗 | C1 / C2 / A3 |
| 推送 | E1–E3 |
| GitHub 上的介绍还写着没有 RAG | C9 |
| CI 四个检查红了三个，为啥 | C4 / C6 / C7 / C8 / C10 |
| 先暂停，把所有问题、Redis、MCP 讲清楚 | 第二、三章 |

---

## 一、问题清单

### 总览

| # | 模块 | 症状 | 一句话根因 |
| --- | --- | --- | --- |
| A1 | 桌面端 | 双击安装包后 20 多秒才出窗口 | 先起服务、再开窗口，两件事串行 |
| A2 | 前端 | 壁纸预设要换 | — |
| A3 | 桌面端 | 关掉窗口再打开，聊天记录全没了 | WebView2 内存配置档 + 随机端口 + 只存在 localStorage |
| B1 | Agent | 用户要 PDF，助手只会说「请点导出按钮」 | 导出只有 HTTP 接口，没有工具 |
| B2 | Agent | 新工具一加就 `ImportError: partially initialized module` | 服务层 ↔ 工具包循环导入 |
| B3 | Agent | 用户同意后，助手把行程重排了一遍 | 提示词没约束「这一轮只调一个工具」 |
| B4 | Agent | 只问「要不要 PDF」，路线图不提 | 提示词只写了选项之一 |
| B5 | 服务层 | PDF 生成 36 秒 | 每天每个地名都重新解析一次 |
| C1 | 打包 | 安装版检索报 `KNOWLEDGE_EMPTY` | 相对路径跟着进程 CWD 走 |
| C2 | 打包 | 上面这个问题冒烟测试没拦住 | 测试在数文件，不是在问运行时 |
| C3 | API | 文件不存在返回 500 | 错误码没进 `CODE_TO_HTTP` 映射表 |
| C4 | PDF | CI 上导出 PDF 全挂 | 字体路径写死成 `C:/Windows/Fonts/simhei.ttf` |
| C5 | 部署 | 容器里检索同样是空的 | `Dockerfile` 漏了 `COPY knowledge` |
| C6 | CI | 类型检查本地过、CI 红 | `_bundled_keys` 是打包时才生成的文件 |
| C7 | CI | 格式检查 8 个文件不合规 | 这些文件从来没被 `ruff format` 过 |
| C8 | 依赖 | `uv lock --check` 失败 | 加了 `desktop` 额外依赖但没重锁 |
| C9 | 文档 | README 一处讲 RAG、一处写「不做 RAG」 | 早期只做工具检索，后来加了 BM25 知识库，文案没跟着改 |
| C10 | CI | 四个检查红了三个 | 三条独立的「本地过、CI 挂」，见逐条对照表 |
| E1 | 协作 | commit 标题多出一个 `@` | 在 bash 里用了 PowerShell 的 here-string |
| E2 | 协作 | 拿不到 CI 失败原因 | GitHub API 未鉴权按 IP 限流 |
| E3 | 协作 | 查 CI 的脚本连不上 | 代理端口硬编码成上一次的值 |

---

### A1 双击后等 20 多秒才出窗口

**症状**：安装版双击之后，20 多秒里屏幕上什么都没有。用户以为没点开，会再点一次，然后开两个实例。

**根因**：启动流程是串行的——

1. 起 uvicorn（导入 LangChain、加载知识库、连 Redis、探测 MCP 子进程……）
2. 等 `/health` 返回 200
3. 才 `webview.create_window()`

第 1 步在冷启动时要十几秒，第 3 步在第 1 步之后，于是窗口出现时间 = 全部初始化时间。

**修法**（`app/desktop.py`）：

- **窗口先出，服务后起**。`create_window` 立刻建，把 uvicorn 扔进 `webview.start(worker)` 的 worker 线程里跑。窗口一出现就先显示「正在启动」的本地占位页，服务好了再导航到真实地址。
- **端口固定**（见 A3），避免每次都等端口协商。
- 顺带把「等健康检查」改成带超时的轮询，而不是固定 sleep。

**验证**：装完之后用 `scripts/probe_startup.py` 打时间戳，窗口出现 `22.8s → 1.3s`，页面可交互 `36.6s → 3.2s`。

---

### A2 壁纸预设替换

纯前端数据改动：`web/app.js` 里的壁纸清单去掉「晨雾海湾」，加「雪峰峡谷」「雨窗伦敦」，图片资源进 `web/assets/`。压缩产物 `web/app.min.js` 同步更新并改版本号，否则浏览器还会用缓存里的旧包。

---

### A3 关掉窗口再打开，历史消息消失

这是这次最值得写的一条，因为它有**三层原因**，只修一层看起来会「好了」，但换个场景又复发。

**第一层：WebView2 的配置档是内存态。**
`webview.start()` 默认 `private_mode=True`，WebView2 会用一次性配置档，窗口一关 localStorage 整个清空。

**修法**：

```python
profile_dir = PROJECT_ROOT / "data" / "webview"
webview.start(worker, private_mode=False, storage_path=str(profile_dir))
```

注意 `PROJECT_ROOT` 而不是 `RESOURCE_ROOT`——配置档是**要写**的，得落在 exe 旁边的可写目录，不能落在 `_MEIPASS` 那个只读临时目录里。

**第二层：localStorage 按「源」隔离，而源包含端口。**
桌面版原来 `bind(("127.0.0.1", 0))` 让系统随机分配端口，每次启动端口不同 → `http://127.0.0.1:56507` 和 `http://127.0.0.1:50660` 是两个不同的源 → 即使配置档保住了，也读不到上个源下的 key。

**修法**：优先固定 `8756`，占用了才退回随机。

```python
PREFERRED_PORT = 8756

def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((HOST, PREFERRED_PORT))
            return PREFERRED_PORT
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])
```

**第三层（真正的修法）：聊天记录不该只存在浏览器里。**
前两层都是在「保住浏览器存储」，但浏览器存储本来就不可靠——用户清缓存、换浏览器、隐私模式，都会丢。**会话的真相必须在服务端**，前端只存一个「上次看的是哪个会话」的指针。

于是补了：

- `ChatStore.list()` 抽象方法，`memory` / `file` / `redis` 三个后端各实现一份；
- `GET /api/v1/chat/sessions`，返回 `{id, title, created_at, updated_at, message_count}`；
- 标题从第一条 user 消息派生（截 20 字加省略号，没有就叫「新的旅程」）；
- 前端启动时先 `loadSessions`，再把 `localStorage` 里的 active session 恢复出来；localStorage 空了就落到最近一条会话上。

**验证**：写了 `scripts/ui_history.py` 真开一次窗口——预置一条会话后启动，断言 landing 页隐藏、侧栏 1 条且标题正确、消息气泡 2 个、正文里能看到用户消息和助手回复。安装版实测 `/chat/sessions` 返回用户原有的 4 条会话（「rag检索」「想去黑龙江玩」「想用一句话介绍你自己」）。

---

### B1~B4 行程交付闭环

用户要的是一条闭环：**排完行程 → 问要不要交付 → 做一个 → 再问另一个**。

#### B1 助手手里没有交付工具

导出能力原本只有 `POST /trips/{id}/pdf` 这个 HTTP 接口，由前端按钮触发。用户说「帮我出一份 PDF」时，模型没有工具可调，只能回一句「请点上面的导出按钮」——话说一半，而且用户已经明确要求了还要自己动手。

**修法**：加两个工具 `export_trip_pdf` / `render_trip_map`（`app/agent/tools/delivery.py`）。

参数设计上有个刻意的取舍：**只要「每天一串地名」**，不要时刻、备注、里程。

> 助手刚排完行程，手上只有这些。要它再填时刻、备注、里程，只会多出编造的机会；这些字段留空，PDF 里就是干净的地点列表，和它在正文里说的对得上。

#### B2 循环导入

新工具一加就炸：

```
app.services.trip_export → app.services.trips → app.agent.tools.info
  → app.agent.tools.__init__ → app.agent.tools.delivery → app.services.trip_export
```

这条链**只在「先 import app.services」时暴露**——线上刚好是相反顺序，所以本地跑得好好的，某些测试一进来就崩。

**修法**：把服务层导入放进函数体内（延迟导入），并在注释里写清这条链：

```python
# 服务层的导入放在函数里：``app.services.trip_export`` → ``app.services.trips``
# → ``app.agent.tools.info`` → 本包的 ``__init__`` → 本模块，顶层导入会绕成环。
# 这条链只有在「先 import app.services」时才暴露，线上刚好相反顺序才没炸。
```

#### B3 用户同意后，模型把行程重排了一遍

第一次实测：用户说「要 PDF」，模型重新调了 `search_knowledge` + `get_weather` + `search_poi` + `plan_trip`，然后才导出。慢十几秒，而且**挑出了另一批景点**——用户拿到的文件和刚才看到的方案对不上，等于白讲一遍。

**修法**：提示词第 21 条明确写死「这一轮只调这一个工具」：

> 用户同意后：要 PDF 就调 `export_trip_pdf`，要路线图就调 `render_trip_map`。这一轮**只调这一个工具**：不要重新查知识库、天气、地点，也不要重排——上文那份就是最终版，直接把它填进工具参数。

#### B4 只问一个选项

第 20 条一开始只写了「问要不要 PDF」，模型就真的只问 PDF，路线图永远没人知道。

**修法**：写死「两个选项都要说到」，并且限定「只问这一句，不要顺带列别的选项，也不要在用户同意之前就调用工具」。第 22 条补「做完一个再问另一个，两个都做完就不再问，用户说不用就停」。

**验证**：端到端三轮实测工具序列——

1. 第 1 轮 `['search_knowledge','get_weather','search_poi','plan_trip']`，且没有提前出文件 ✓
2. 第 2 轮 `['export_trip_pdf']` ✓
3. 第 3 轮 `['render_trip_map']`，且不再追问 ✓

#### B5 PDF 生成 36 秒

`plan_route` 对每个地名都走一次地理编码，一天的行程里有重复地名（酒店、餐厅来回出现），就重复请求。

**修法**：加进程内地名缓存，同一次导出内命中即复用。`36s → 14.9s`。

---

### C1 打包版检索报 `KNOWLEDGE_EMPTY`

**症状**：源码跑一切正常，装完之后问任何需要资料的问题，都回「知识库为空（未找到语料文件）」。

**根因**：`.env` 里 `KNOWLEDGE_DIR=knowledge` 是**相对路径**，而 `Path("knowledge")` 跟着**进程 CWD** 走：

| 运行形态 | CWD | `Path("knowledge")` 解析到 | 片段数 |
| --- | --- | --- | --- |
| 源码 | 项目根 | `<项目>/knowledge` | 23 |
| PyInstaller | exe 所在目录 | `<安装目录>/knowledge`（不存在） | 0 |

语料其实好好地躺在 `_internal/knowledge` 里——只是没人去那儿找。

**修法**：区分两种根，并在解析层强制区分（`app/core/paths.py`）：

- `PROJECT_ROOT`：**可写**目录（`.env`、`data/`、`media/`），冻结形态 = exe 所在目录；
- `RESOURCE_ROOT`：**只读**资源（`web/`、`knowledge/`），冻结形态 = `sys._MEIPASS`。

```python
def resolve_resource(path: str | Path) -> Path:
    """把配置里的**只读资源**路径解析成绝对路径。

    相对路径一律按 RESOURCE_ROOT 算，不能留给 Path(...) 去跟进程 CWD——
    打包后 CWD 是 exe 目录，语料在 _internal 里，一跟就错。
    """
    raw = Path(path).expanduser()
    return raw if raw.is_absolute() else RESOURCE_ROOT / raw
```

调用点改成 `get_knowledge_base(resolve_resource(settings.knowledge_dir))`。

**为什么不用「启动时 chdir 到 _MEIPASS」这种省事的办法**：那会把可写目录也一起带偏，`.env` 和 `media/` 会写进临时目录，退出即失。

---

### C2 冒烟测试没拦住 C1

原来的冒烟测试是「数 `_internal/knowledge` 下有几个 `.md`」——它数的是**包里的文件**，而 bug 在**运行时的解析**。文件一直在，测试就一直绿。

**修法**：改成「问运行时」——起服务，打 `/health`，断言 `knowledge_chunks > 0`：

```python
status, body = _fetch(f"http://127.0.0.1:{port}/api/v1/health")
chunks = int(json.loads(body).get("knowledge_chunks", 0))
if chunks <= 0:
    raise SystemExit(f"健康检查说知识库有 {chunks} 个片段：语料在包里但运行时读不到")
```

同时给 `/health` 加了 `knowledge_chunks` 字段。**这个字段的价值不只在冒烟测试**——它是「检索到底能不能用」的唯一可观测信号，用户报「检索失败」时看一眼就知道是语料没进包、还是路径错了、还是索引建失败。

> 这条的教训比 C1 本身重要：**测试要断言行为，不要断言布置**。数文件是断言布置。

---

### C3 文件不存在返回 500

**症状**：`GET /api/v1/trips/files/trip-xxx.pdf`（文件已被清理）返回 500，前端弹「服务器开小差了」。

**根因**：代码里抛的是 `ApiError("NOT_FOUND", ...)`，但 `CODE_TO_HTTP` 里没有 `NOT_FOUND` 这个键，查不到就落到默认的 500。

**修法**：补映射。改完立刻触发了契约测试 `test_error_contract.py::test_every_backend_code_has_frontend_text`——要求**每个后端错误码在前端都有对应文案**。于是 `web/app.js` 补文案、`app.min.js` 同步、版本号加一。

> 这个契约测试是这次改动里最有价值的东西：它把「后端加了码、前端没文案」这种静默降级变成红灯。

---

### C4 字体：reportlab 不支持 CFF 轮廓

**症状**：CI 上所有导出 PDF 的用例挂掉，本地全绿。

**根因**：字体路径写死 `C:/Windows/Fonts/simhei.ttf`，Linux runner 上没有。

但改的时候撞上第二个坑：直接把候选列表里加上 Noto CJK，`pdfmetrics.registerFont` 抛异常——**reportlab 的 `TTFont` 只认 TrueType 轮廓，不认 CFF/PostScript 轮廓**，而 `NotoSansCJK-Regular.ttc` 是 CFF。

**修法**：

1. 候选表按平台铺开，**文泉驿（TrueType）排在 Noto 前面**；
2. 加 `_try_register()` 真去试一次，失败就换下一个，并把异常降到 debug 日志；
3. `.ttc` 要带 `subfontIndex=0`，否则读不出来；
4. CI 和 Docker 统一装 `fonts-wqy-zenhei`。

```python
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # CFF，可能失败
    ...
    "C:/Windows/Fonts/simhei.ttf",
)

def _try_register(path: Path) -> bool:
    if FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return True
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(path), subfontIndex=0))
    except Exception as exc:  # 轮廓格式不支持、文件损坏、权限不足，都走这里
        logger.debug("字体 %s 注册失败：%s", path, exc)
        return False
    return True
```

测试侧加 `skipif(not _HAS_CJK_FONT)`：环境真没中文字体时跳过，而不是判失败——否则开发机上跑测试会一片红，久了就没人看测试了。

---

### C5 Dockerfile 漏 `COPY knowledge`

和 C1 是同一个病的另一个发病部位：镜像里 `app/` 有、`knowledge/` 没有，于是容器内检索也是空的。

**修法**：补 `COPY knowledge ./knowledge`，装中文字体，并把容器冒烟断言从「服务起来了」升级成 `knowledge_chunks > 0`。

---

### C6 `_bundled_keys`：本地过、CI 红

**症状**：`mypy` 本地 `Success: no issues found`，CI 报

```
app/core/secrets.py:121: error: Module "app.core" has no attribute "_bundled_keys"  [attr-defined]
```

**根因**：`app/core/_bundled_keys.py` 是**打包脚本生成**的文件（把内置 key 编进去），被 `.gitignore` 排除。本地因为打过包所以存在，CI 上根本不存在。静态 `from app.core import _bundled_keys` 在有文件时能过、没有时炸。

**修法**：改成运行时动态导入，文件不存在就当空字典：

```python
def bundled() -> dict[str, str]:
    try:
        module = importlib.import_module("app.core._bundled_keys")
    except ImportError:
        return {}
    loader = getattr(module, "keys", None)
    if not callable(loader):
        return {}
    try:
        return {str(k): str(v) for k, v in loader().items()}
    except Exception:
        logger.exception("内置配置解析失败，已忽略")
        return {}
```

**验证**：`mv app/core/_bundled_keys.py /tmp` 模拟 CI 环境，再跑 `mypy app` → `Success: no issues found in 108 source files`。

> 更普遍的教训：**「本地能过」不等于「代码对」，只等于「本地有这个文件」**。任何被 gitignore 的生成物，都不该被静态 import。

---

### C7 ruff format：8 个文件从来没格式化过

`ruff format --check` 报 8 个文件不合规。第一反应是「我这次改坏了」，但用 `git show HEAD:app/agent/tools/system.py` 取出改动前的版本一跑，同样不合规——说明这些文件在加 `ruff format --check` 这条 CI 之前就没格式化过，只是以前没人检查。

**修法**：先确认「不是我改坏的」，再统一 `ruff format .`，单独一个提交，不和逻辑改动混在一起（否则 review 时看不出哪行是格式、哪行是逻辑）。

---

### C8 `uv lock --check` 失败

加了 `desktop` 额外依赖（pywebview）但没重锁，锁文件里没有 pywebview。

顺带实测了一个反直觉的结论：**锁文件过期并不影响 `uv sync --frozen`**（exit 0），所以它不是 CI 红的直接原因。但 `uv lock --check` 会失败，该补还是得补。

---

### C9 README 自相矛盾：「有 RAG」和「不做 RAG」同时写着

**症状**：同一份 README 里，第 13 节标题是「RAG：知识库存规则」，另一节却写着「不做 RAG，也不建向量库」。你看到的就是「介绍里说没有 RAG」。

**根因**：项目早期确实只做工具式检索、没建索引；后来加了 `app/rag/`（BM25，`knowledge/` 下的 23 篇语料），那句「不做 RAG」是历史遗留。**项目实际做的是「有 RAG，但不建向量库」**——这两件事被混成了一句话。

**修法**：那一节改成：

```markdown
### 有 RAG，但不建向量库

简历上的 Agent 项目几乎都有「RAG + 向量库」，这里只做前一半。
```

保留原来「时效性信息不能进知识库」的论证（那是这段最有价值的部分），只是把「不做 RAG」这个错误结论换掉。

顺带校准了一批过期数字——它们和上面那句是同一个病，都是「代码变了、文案没变」：

| 位置 | 旧 | 新 |
| --- | --- | --- |
| 开头一句话 | 23 个工具 | 25 个工具 |
| 能力表 | 23 个全开 | 25 个全开（补「行程交付」） |
| 架构图 | `plan 5`，无 `knowledge` | `plan 7`，补 `knowledge 1` |
| 硬伤表 | 22 个工具全部走真实数据源 | **涉及外部数据的工具**全部走真实数据源 |
| 评测一节 | 全部 23 个工具 | 全部 25 个工具 |
| 测试数 | 618 | 725 |

最后一行的改法值得单独说：原文「22 个工具全部走真实数据源」在加了 `export_trip_pdf` / `render_trip_map` / `list_dir` 这些**不联网**的工具后就不成立了。与其把数字改成 25（那又变成假话），不如改成「涉及外部数据的工具」——**把可数的数字换成不可数的限定语，这类文案就不会再腐烂**。

> 教训：文档里的数字是最容易烂的东西。凡是「N 个工具」「N 个测试」这种可数事实，要么在 CI 里断言，要么别写具体数。

---

### C10 CI 四个 job 红三个：逐条对照

**症状**：推上去之后 Actions 里 `Lint & format`、`Type check`、`Test (3.12)`、`Test (3.13)` 全红，`Eval harness` 和 `Docker build` 绿。

三个红是**三条互不相干的原因**，这也是为什么「本地全绿」这件事一点参考价值都没有——它们各自依赖一个本机恰好存在的前提。

| Job | 报错 | 根因 | 修法 |
| --- | --- | --- | --- |
| Lint & format | 8 个文件 would be reformatted | 这些文件在加 `format --check` 之前就没格式化过 | `ruff format .`，单独一个提交（见 C7） |
| Type check | `Module "app.core" has no attribute "_bundled_keys"` | 该文件是打包时生成、被 gitignore 的 | 改 `importlib` 动态导入（见 C6） |
| Test | 导出 PDF 的用例抛「找不到中文字体」 | 字体路径写死 Windows 路径 | 候选表 + 跳过标记 + CI 装 `fonts-wqy-zenhei`（见 C4） |
| Docker build（绿，但差点漏） | 健康检查 200、检索却是空的 | 镜像漏 `COPY knowledge` | 补 COPY + 断言 `knowledge_chunks > 0`（见 C5） |

**定位手段**（这一步比修法本身更值得记）：日志下载要鉴权，而 **Checks 页面上的注解是公开可读的**。所以给 mypy 那一步加了一段 `sed`，把 stderr 转成 GitHub 注解：

```yaml
- name: mypy
  run: |
    set +e
    uv run --frozen mypy app evals --show-error-codes > mypy.out 2>&1
    code=$?
    cat mypy.out
    sed -n 's/^\([^:]*\):\([0-9]*\): error: \(.*\)$/::error file=\1,line=\2::\3/p' mypy.out | head -50
    exit $code
```

改完之后，失败原因直接显示在 job 页面的 Annotations 区，不用登录也能看。**「本地过、CI 挂」这类问题的排查成本，八成花在「看不到真实报错」上**，而不是花在修上。

Docker 那条同样从「进程活着」升级成「功能可用」：

```bash
echo "$body" | grep -q '"knowledge_chunks":[[:space:]]*[1-9]' \
  || { echo "知识库为空：镜像里没带 knowledge/ 或路径解析错了"; docker logs api; exit 1; }
```

---

### D 组：几个小坑

- **`ruff check --fix` 会顺手删掉无用的 `noqa`**，然后格式检查又要求它——两个工具来回拉锯。正确顺序是先 `--fix` 再 `format`，一次到位。
- **`mypy` 不喜欢 `dict[str, Any]` 排序**：`list_sessions` 里 `sorted(..., key=lambda r: r["updated_at"])` 因为值类型是 `Any` 报错。改成 `TypedDict _SessionRow` 显式声明字段类型就过了——顺带让返回值结构自解释。
- **在 bash 里用 PowerShell 的 here-string 写 commit message**，标题会多出一个 `@`。详见 E1。

---

### E 组：推送过程中的三个坑（不是产品 bug，但每次都耽误时间）

#### E1 commit 标题多出一个 `@`

**症状**：`git log` 里看到 `@docs(readme): ...`。

**根因**：在 bash 里写了 PowerShell 的 here-string：

```bash
git commit -m @'
docs(readme): ...
'@
```

bash 不认识 `@'...'@`，把开头的 `@` 当成字面量并进了消息。

**修法**：消息写进文件，用 `-F`：

```bash
git commit -F build/msg-readme.txt
git commit --amend -F build/msg-readme.txt   # 已经提交错了就改
```

**为什么不用 `--amend` 反复试**：amend 会换 commit hash，推过的分支再改就得 force push。所以消息先落在文件里、看一遍再提交，比事后补救省事。

#### E2 GitHub API 限流，拿不到 CI 失败原因

**症状**：`rate limit exceeded for <ip>`——查 Actions 状态时被挡。

**根因**：未鉴权的 REST 请求按 **IP** 限流，60 次/小时。排查 CI 时往往要连查好几个接口（runs → jobs → logs），很快就用完。

**修法**：改抓**公开 HTML 页面**——Actions run 页 → job 页 → 页面里的 `<job-annotations>` 块。这条路不需要 token，且注解本来就是给未登录用户看的。

> 顺带一个结论：**如果你需要反复查 CI，第一步应该是把报错搬到注解里**（C10 那段 `sed`），而不是想办法多拿几次 API 额度。

#### E3 代理端口硬编码，脚本连不上

**症状**：查 CI 的脚本昨天能跑，今天 `connection refused`。

**根因**：端口被写成了上一次看到的 `56507`，而代理软件每次重启都换端口（`56507` → `50660`）。

**修法**：不写端口，直接用环境变量里的 `https_proxy`：

```python
proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
```

**教训**：任何「我上次看到是这个值」的东西——端口、路径、hash——都不该进代码。要么从环境读，要么当场算。

---

## 二、Redis：在这个项目里到底干什么

### 一句话定位

**把「进程内的状态」搬到「进程外」，让多 worker、重启、跨进程这三件事同时成立。**

Redis 在这个项目里**不是缓存**，一处缓存都没做。四处用法全是「共享状态 + 通知」，而且每一处都同时有一个零依赖的本地实现作为降级。

| 用途 | 数据结构 | Key / Channel | 本地降级 |
| --- | --- | --- | --- |
| SSE 事件流 | List + Hash + Pub/Sub | `chat:stream:{id}:events` / `:meta` / `:wake` | `MemoryEventLog` |
| 会话存储 | String（单 key 一段 JSON） | `chat:session:{id}` | `MemoryChatStore` / `FileChatStore` |
| 媒体任务元数据 | String + TTL | `media:task:{id}` | `NullMediaTaskStore` |
| 权限裁决 | String + Pub/Sub | `perm:{request_id}` / `perm:{id}:decision` | `LocalDecisionChannel` |

### 1. SSE 事件流：为什么用 List，不用 Redis Stream

一轮回答产生的是一条**事件流**：`token`、`tool_call`、`tool_result`、`permission_request`……前端用 SSE 连上来，断线后带 `Last-Event-ID` 续传。

关键在于「续传」：浏览器重连时可能落到**另一个 worker 进程**上，那个进程的内存里没有这条流。所以事件必须存在共享存储里。

**为什么是 List 而不是 Redis Stream（XADD/XRANGE）**：

> 游标就是列表下标，和 SSE 的 `Last-Event-ID` 一一对应，`LRANGE key cursor -1` 一次取完。换成 XADD/XRANGE 的话事件 id 是 `毫秒-序号` 字符串，续传前还得把客户端传来的整数翻译成 stream id，多一层换算就多一处能错的地方。这里要的是「按序重放」，不是「消费组 + ACK」。

具体命令：

```python
# 追加：RPUSH 返回新长度，这个长度天然就是 seq
seq = int(await self._client.rpush(events_key, json.dumps(payload, ensure_ascii=False)))
await self._client.expire(events_key, self._ttl)          # 每条都续期
await self._client.publish(self._wake_key(stream_id), str(seq))  # 只负责叫醒

# 订阅：LRANGE 一次取完未读部分
items = await self._client.lrange(events_key, cursor, -1)
```

`meta` 是个 Hash（`session_id` / `done` / `cancelled`），`wake` 是 Pub/Sub 频道。

**为什么 Pub/Sub 只做唤醒、不做真相**：

> Pub/Sub 会丢消息（订阅前发布、断线期间发布），所以每次被唤醒都回 List 重读一遍。唤醒丢了也不致命——最坏退化成心跳间隔的轮询。

也就是说：**List 是唯一真相，Pub/Sub 只是个「别睡了」的门铃**。门铃没响，最多晚 15 秒（`HEARTBEAT_SEC`）。

**常量与它们的来由**：

| 常量 | 值 | 为什么是这个值 |
| --- | --- | --- |
| `STREAM_TTL_SEC` | 3600 | 够用户断线重连、刷新页面即可，不必留一天 |
| `MAX_STREAMS` | 200 | 单进程最多保留多少条流，超出按最后更新时间淘汰 |
| `HEARTBEAT_SEC` | 15.0 | 公网反代会掐断长时间无数据的 SSE 连接 |
| `CANCEL_POLL_SEC` | 1.0 | 生成端多久检查一次「别的进程有没有取消我」 |

**已知代价**（写进了 README 的已知限制）：流状态有 TTL，过期后无法续传，只能提示刷新会话；但**已完成的回答在会话存储里**，不会因此丢失——这就是为什么「流」和「会话」要分开存。

### 2. 会话存储：为什么整段 JSON 塞一个 key

`chat:session:{session_id}` → 一段 JSON（含 messages 列表）。

听起来很粗糙，但这是刻意的：

- 会话是**整体读、整体写**的，没有「只改第 3 条消息」这种需求；
- 拆成 Hash 或 List 反而要处理「消息追加到一半崩了」的部分写入问题；
- 单 key 写入在 Redis 里是原子的，天然没有这个问题。

代价是**大会话序列化开销**，所以实现里做了两件事：序列化结果做进程内缓存（内容没变就不重新 `json.dumps`），写盘走 `asyncio.to_thread`（文件后端）避免阻塞事件循环。

### 3. 媒体任务元数据：`SET ... EX`

> 任务本身只在内存里跑，但**元数据**必须跨进程重启保留，否则历史会话里的图片卡片会查不到状态。

`media:task:{id}` → `{status, url, prompt, ...}`，TTL 1 小时。读写全部 `try/except` 包住——存储故障不该让「查一下这张图好了没」变成 500。

### 4. 权限裁决：`SET pending EX` + Pub/Sub + 1 秒轮询

这是四处里设计最讲究的一处，因为它原本是**模块级 dict + `asyncio.Future`**，单进程最省事：

```python
# 改之前
_pending: dict[str, PermissionBroker] = {}
```

问题：`uvicorn --workers > 1` 时，前端那次 `POST /permissions/{id}/decision` **大概率落到另一个进程**——那个进程的 dict 里没有这条请求，于是 404；真正在等的那台机器一直等到 120 秒超时。**用户点了「允许」也没用。**

改成一个 `DecisionChannel` 抽象：

- `LocalDecisionChannel`：进程内 Future，零依赖；
- `RedisDecisionChannel`：key 存裁决结果 + Pub/Sub 唤醒。

key 有三种状态，这个三态设计是整个方案的核心：

| key 状态 | 含义 |
| --- | --- |
| 不存在 | 没有这个请求 |
| `"pending"` | 请求在等裁决 |
| `{"granted":true,"remember":false}` | 已裁决 |

```python
# 请求方：先登记 pending（带 EX，防止进程崩了留下永久 pending）
await self._client.set(self._key(request_id), "pending", ex=DECISION_TIMEOUT_SEC + 30)

# 等待方：先查一次 key —— 裁决可能比订阅更早到（覆盖竞态）
# 然后订阅频道，被唤醒或每 1 秒回读一次 key
# 裁决方：写 key + publish
await self._client.set(key, decision.to_json(), ex=DECISION_TIMEOUT_SEC + 30)
await self._client.publish(self._channel(request_id), decision.to_json())
```

> 等待方先查 key 再订阅，覆盖「裁决比订阅先到」这个竞态；订阅只是让延迟变低，**真正的真相永远是 key**。这样即使 Pub/Sub 丢消息，最多慢一秒，不会丢裁决。

这个「先查再订阅 + 定期回读」的模式，和 SSE 那边「Pub/Sub 只做唤醒」是同一个思路的两次应用。可以总结成一条项目内的原则：

> **Redis 里凡是会丢的通道（Pub/Sub），都只用来降低延迟；不丢的存储（String/List/Hash）才是状态本身。**

### 5. `app/core/redis.py`：为什么单独一个 35 行的文件

因为它只解决一件事：**超时必须显式设**。

> redis-py 8 把默认 socket 超时从「不超时」改成了 5 秒。本机实测第一次连接要 2 秒左右，冷启动时「建连接 + 第一条命令」很容易一起越过 5 秒，于是重启后第一个请求直接超时——SSE 那边表现成「200 但一个事件都没有」，会话存储那边表现成 500，都不好查。

所以统一放宽到 10 秒，连接超时和读超时分开写。

还有一个反直觉的取舍：**不加 `retry_on_timeout`**。

> Pub/Sub 的读是阻塞式的，重试会把「这一轮确实没有消息」变成一次多余的往返。

### 6. 为什么桌面版默认不是 Redis

单用户、单进程、单机——Redis 带来的全是安装负担，没有一条收益。所以默认后端是 `file`（原子写 JSON），Redis 是给 `docker-compose` 多 worker 部署准备的。

这个选择有个副作用：**两个后端的语义必须一致**，否则「本机好的、服务器坏」会成为常态。所以 `ChatStore` / `EventLog` / `DecisionChannel` / `MediaTaskStore` 都先定义 Protocol，再各写两份实现，测试对两个实现跑同一批用例。

---

## 三、MCP：双向实现与用途

MCP（Model Context Protocol）解决的是**「工具怎么跨进程、跨产品复用」**。这个项目里它是**双向**的：

```
                    ┌─────────────────────────┐
   Claude Desktop   │  app/mcp/server.py      │   ← 我们把工具卖出去
   Cursor       ───▶│  (stdio, JSON-RPC 2.0)  │
                    └───────────┬─────────────┘
                                │ 复用 ALL_TOOLS
                    ┌───────────▼─────────────┐
                    │  25 个内置工具           │
                    └───────────▲─────────────┘
                                │ all_tools() = 内置 + 外部
                    ┌───────────┴─────────────┐
                    │  app/mcp/registry.py    │   ← 我们把别人的工具买进来
                    └───────────┬─────────────┘
                                │ stdio 子进程
                    ┌───────────▼─────────────┐
   MCP_SERVERS 配置  │  npx @modelcontext…     │
                    └─────────────────────────┘
```

### 用途一：服务端 —— 把 25 个工具里的 20 个卖出去

一行命令就能跑起来：

```bash
python -m app.mcp.server
```

Claude Desktop / Cursor 配置：

```json
{"mcpServers": {"ocean": {"command": "python", "args": ["-m", "app.mcp.server"]}}}
```

或者手动喂一行 JSON 验证：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{...}}}' | python -m app.mcp.server
```

**为什么不暴露写操作**——这是这个模块里最重要的一个决定：

> 文件写、删、换壁纸这些工具在应用内依赖 `PermissionBroker`——它把确认请求推给前端 SSE，等用户点「允许」。MCP 场景里没有那个前端，`require_permission` 找不到裁决通道就一律拒绝（这是刻意的失败方向）。于是把这些工具挂上 MCP 只有一个结果：客户端看到一堆工具，调了必然 `PERMISSION_DENIED`。
>
> 「能列出但永远失败」比「不列出」更糟——客户端会反复尝试，用户看到的是这个服务坏了。

所以按 `DANGEROUS_TOOLS` 过滤：25 个工具暴露 20 个，过滤掉 `write_file` / `create_dir` / `delete_path` / `set_wallpaper` / `open_file`。

**复用同一份清单，不另写一份**：

> 过滤依据是注入防护里那份 `DANGEROUS_TOOLS`，不另写一份——两份清单漂移的后果是「这层以为只读、那层其实会写盘」。

### 用途二：客户端 —— 把外部服务端的工具并进 `ALL_TOOLS`

配置格式**故意跟 Claude Desktop 的 `mcpServers` 一致**，用户可以把手上的配置直接抄过来：

```json
MCP_SERVERS=[{"name":"fs","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}]
```

为什么要有 `registry.py` 这一层，而不是直接改 `ALL_TOOLS`：

> `ALL_TOOLS` 是模块级常量，被工具清单接口、Agent 构图、评测三处引用。外部工具是**运行时**才知道的（要连子进程、要 `tools/list`），而且可能一个都没有。所以保留常量作为「内置工具」，外面套一层 `all_tools()` 返回「内置 + 外部」。

**失败必须吞掉**：

> MCP 服务端是可选依赖：命令写错、包没装、进程秒退，全都是现实里会发生的事。这些情况下正确的行为是「少几个工具，应用照常起来」，而不是整个服务启动失败。

坏一条只跳过那一条——一条写错不该让其余服务端都连不上。

### 协议：2026-07-28 那次变化，是最容易写错的地方

| | 2025-11-25 及更早 | 2026-07-28 起 |
| --- | --- | --- |
| 握手 | 先 `initialize`，服务端回 `protocolVersion` | **取消握手，无状态** |
| 版本怎么带 | 连接级状态 | 每个请求在 `params._meta` 里带 `protocolVersion` / `clientInfo` / `clientCapabilities` |
| 发现 | 无 | 新增 `server/discover`（服务端 MUST 实现） |

错误码也有讲究：

- 缺 `protocolVersion` 或 `clientCapabilities` → `-32602`（Invalid params）
- 版本不认识 → `-32022`（Unsupported protocol version），**并且必须附上自己支持的版本列表**

**探测策略**（`client.py::_negotiate`）：先发 `server/discover`，拿到 `DiscoverResult` 就是新版；拿到别的错误或超时就是老版，退回 `initialize` 握手。

> **不能只认某个特定错误码**——老服务端对未知方法的报错是实现自定的。

### stdio 客户端的四个坑，每个都能让程序「看起来正常但什么都不工作」

1. **必须排空 stderr。** 子进程 stderr 是管道，缓冲区满了（Linux 64KB / Windows 约 4KB）子进程会阻塞在写日志上，表现是「请求发出去永远没回」。所以要起一个常驻任务一直读，只保留最后 40 行用于报错。
2. **必须按 id 关联。** stdout 是单通道，响应和通知混在一起。不按 id 分发，并发两个请求就会互相拿错结果——**而这种错在串行测试里永远不会暴露**。
3. **必须探测协议年代**（见上）。
4. **超时后要发 `notifications/cancelled`。** 否则服务端还在算，而我们已经放弃了；连开几轮就会攒下一堆僵尸计算。

另外还有一条容易忽略的：`call_timeout` 默认 120 秒。如果子进程崩溃了，你要等满 120 秒才看到「没有响应」——真因是崩溃，报出来的是超时。所以 `_fail_pending()` 会在读线程发现 EOF 时立刻把所有 pending 请求判失败，并把 stderr 尾巴附在错误信息里。

### 为什么不装官方 SDK

> 协议本体很小，SDK 会带进一整层传递依赖。代价是这里要自己处理上面四条，而它们恰好是最容易写错的地方。

三个模块加起来 1189 行（protocol 200 / client 589 / server 397 + registry 192 里的一部分），没有新增任何第三方依赖——只用 `asyncio` + `json`。

这个取舍在「桌面应用要塞进安装包」的场景下是划算的：多一个依赖就多一份体积和一份冲突可能。

---

## 四、还没做完的

| 事项 | 状态 |
| --- | --- |
| `Type check` / `Test` 两个 CI job | 修复代码已写好并本地验证（mypy 108 文件通过、相关 41 个用例通过），**5 个文件未提交** |
| 撤销授权界面 | 只有接口，前端没入口 |
| `.env.example` 的 `MEDIACRAWLER_TIMEOUT=60` | 与注释「B站约 147s」矛盾，待裁决 |
| `docs/frontend-handoff/`、`docs/marketing/` 里的「默认 Redis」 | 桌面版实际是 `file`，未校准 |

---

## 五、如果要总结成三条

1. **路径、字体、生成物——凡是「本机能跑」的东西，都要问一句「CI 上它从哪来」。**
   C1（相对路径跟 CWD）、C4（写死 Windows 字体）、C6（gitignore 的生成文件被静态 import），三个 bug 是同一个形状。

2. **测试要断言行为，不要断言布置。**
   C2 那条「数 md 文件」的冒烟测试，在有 bug 的情况下依然全绿——因为它检查的是「东西在不在包里」，而不是「运行时能不能用」。

3. **会丢消息的通道只用来降延迟，不丢的存储才是状态。**
   SSE 的 Pub/Sub、权限裁决的 Pub/Sub，两处都是这个模式：先查存储，再订阅，被唤醒后回读存储。
