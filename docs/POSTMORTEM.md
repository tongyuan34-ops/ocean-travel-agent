# 工程复盘：两次「本地能过、CI 才暴露」的事故

> 记录于 2026-10-24。这不是故障报告，而是**在给项目补 CI 的过程中，CI 反过来抓出的两个真实缺陷**。
> 两次都有同一个特征：Windows 上永远复现不了，且第一次的修复被工具链静默回滚了。

- 仓库：`tongyuan34-ops/ocean-travel-agent`
- 首次接入 CI 的提交：`af15304`
- 最终全绿的运行：[CI #3](https://github.com/tongyuan34-ops/ocean-travel-agent/actions/runs/40082782183)（6m15s，五个 job 全过）

---

## 摘要

| # | 现象 | 根因 | 严重度 |
|---|---|---|---|
| 一 | `Type check` job 26 秒即失败 | `os.startfile` 是 Windows 独有 API，Linux 类型桩里不存在；**而此前的"修复"被 ruff 的 B009 自动改写回原样** | 中（阻断流水线） |
| 二 | `Test` matrix 两个 job 永久卡住，连 Cancel 都收不到 | `os.killpg(os.getpgid(pid), SIGKILL)` 打到了**自己**的进程组，把 runner 的 job 进程一起杀了 | **高（生产事故级）** |

第二个事故如果在生产环境触发，后果是：**MediaCrawler 只要超时一次，Linux 上的后端服务就会把自己 SIGKILL 掉。**

---

## 事故一：类型检查在 Linux 上失败

### 现象

`af15304` 触发的 CI #1，`Type check` job 在 26 秒后退出码 1：

```
app/agent/tools/system.py:153: error: Module has no attribute "startfile"  [attr-defined]
Found 1 error in 1 file (checked 73 source files)
```

同一份代码在本地 Windows 上 `mypy app` 是干净的。

### 根因

`os.startfile` 是 Windows 专有 API，typeshed 里被平台条件守卫：

```python
# typeshed 的 os.pyi（示意）
if sys.platform == "win32":

    def startfile(path: StrOrBytesPath, operation: str | None = ...) -> None: ...
```

CI 跑在 `ubuntu-latest`，mypy 按 Linux 解析 `os` 模块，自然没有这个属性。

### 为什么本地没发现 —— 以及一个更隐蔽的坑

原始代码本来是有保护的：

```python
os.startfile(str(target))  # type: ignore[attr-defined]
```

问题出在项目开了 `warn_unused_ignores = true`。在 Windows 上这个 `type: ignore` 是**多余**的，本地反而报：

```
error: Unused "type: ignore" comment  [unused-ignore]
```

为了消掉这个本地报错，当时的改法是绕开属性访问：

```python
getattr(os, "startfile")(str(target))  # 意图：让 mypy 查不到这个属性
```

**然后 ruff 把它改回去了。** 实测复现：

```python
# 改写前
getattr(os, "startfile")(p)

# ruff check --fix 之后
os.startfile(p)
```

触发规则是 **B009**（flake8-bugbear）：*Do not call `getattr` with a constant attribute value*。
它的判断是「`getattr(x, "字面量")` 与 `x.字面量` 等价，可自动修复」。

于是真实发生的事是：

```
改成 getattr → ruff --fix 改回 os.startfile → 本地 mypy 过（Windows）
→ 推 CI → Linux 挂 → 看起来像"平台差异"，其实是修复根本没落地
```

这里最值得记住的一点：**linter 的自动修复把「规避写法」还原成了「出问题的写法」，全程没有任何提示**。当时看到 `All checks passed!` 就以为改完了。

### 修复

```python
if is_windows():
    # 必须用**带默认值**的 getattr：写成 getattr(os, "startfile") 会被
    # ruff 的 B009 自动改回属性访问，mypy 在 Linux 上就会报 attr-defined。
    opener = getattr(os, "startfile", None)
    if opener is None:  # pragma: no cover - is_windows() 为真时一定存在
        return _failed("当前系统不支持用默认程序打开文件", "OPEN_FAILED")
    opener(str(target))
```

带默认值时语义不再等价（可能返回默认值），B009 就不会触发。实测确认：

```
getattr(os, "startfile")         → 被改成 os.startfile
getattr(os, "startfile", None)   → 原样保留
```

### 更重要的修复：让本地和 CI 用同一个平台视角

```toml
[tool.mypy]
python_version = "3.12"
# 部署目标是 Linux 容器，本地也按 linux 平台检查，否则 Windows 上能过、
# CI 上挂掉的平台差异（如 os.startfile、signal 常量）要等推送后才发现
platform = "linux"
```

这条比单个函数的修复价值大得多：**它把「等 CI 告诉你」变成「保存时就告诉你」**。

---

## 事故二：测试任务永久卡死，连取消都收不到

### 现象

CI #1 里 `Lint`（10s）、`Type check`（26s）、`Docker build`（1m25s）都正常结束，唯独 `Test` matrix 显示 `0/2 jobs completed`，持续 20 分钟没有任何进展。

几个反常点：

1. 单个测试套件本地跑完约 50 秒，CI 上不可能超过 3 分钟
2. 运行页面一直显示 `In progress`，**不报错、不超时、不出日志**
3. `concurrency` 配了 `cancel-in-progress: true`，但后续 push 触发的 run #2 只是「排队等待」，并没有取消 #1
4. 手动点 Cancel 后，#1 依然挂着，最后是强制的

第 3、4 点是最强的线索：**一个正常的 job 收到取消信号会立刻退出。收不到信号，说明执行它的进程已经不在了。**

### 定位过程

因为 CI 日志拉不下来（`api.github.com` 被限流、`r.jina.ai` 被拒），只能从「哪些代码只在 Linux 上跑」反推。

`tests/test_subprocess.py` 里有一条用例专门验超时清理：

```python
run = await run_crawler_subprocess(
    [sys.executable, "-c", "import time; time.sleep(60)"], cwd=".", timeout=3
)
```

它必然走到 `_kill_process_tree` 的 POSIX 分支：

```python
# 修复前
if sys.platform == "win32":
    ...  # taskkill /F /T
    return
with contextlib.suppress(OSError, ProcessLookupError):
    os.killpg(os.getpgid(pid), signal.SIGKILL)
```

### 根因

`asyncio.create_subprocess_exec` **不会**给子进程创建新会话/新进程组，子进程继承父进程的进程组。于是：

```
os.getpgid(pid)  →  返回的不是子进程的组号，而是【当前进程自己】的组号
os.killpg(那个组号, SIGKILL)  →  把 pytest 连同 runner 的 job 进程一起杀掉
```

CI 上就是 pytest 自杀 → runner 的 job 进程消失 → job 既不失败也不结束，卡在 `In progress`，连取消信号都无处可送。

### 为什么 Windows 永远复现不了

两个平台的清理策略完全不同：

| 平台 | 实现 | 是否会误伤自己 |
|---|---|---|
| Windows | `taskkill /F /T /PID <pid>` | 不会，`/PID` 精确指定目标 |
| POSIX | `killpg(getpgid(pid))` | **会**，因为子进程没独立成组 |

本地开发全程在 Windows，这条分支一行都没执行过。

### 生产影响

这不是"测试环境才有的问题"。同一个函数在生产里被 `MediaCrawlerAdapter` 调用：

```
采集超时 → run_crawler_subprocess 捕获 TimeoutError → _kill_process_tree(pid)
         → killpg(自己的进程组) → FastAPI 服务被 SIGKILL
```

也就是说：**用户点一次「采集小红书」，平台响应慢导致超时，整个后端进程就没了。** 而且因为是被 SIGKILL，不会有任何异常栈、不会有优雅关闭、不会有日志——运维看到的现象是"服务莫名其妙重启"。

### 修复

**第一层：让子进程独立成组**

```python
# POSIX 上必须让子进程成为独立会话的首进程。
# 否则它继承本进程的进程组，_kill_process_tree 里的 os.getpgid(pid)
# 拿到的就是**我们自己**的组号，killpg 会把 API 服务/测试进程一起 SIGKILL 掉。
spawn_kwargs: dict[str, Any] = {}
if sys.platform != "win32":
    spawn_kwargs["start_new_session"] = True

process = await asyncio.create_subprocess_exec(
    *cmd,
    cwd=cwd,
    env=env,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    **spawn_kwargs,
)
```

**第二层：防御性兜底**

```python
try:
    pgid = os.getpgid(pid)
except (OSError, ProcessLookupError):
    return

if pgid == os.getpgid(0):
    logger.error(
        "拒绝清理进程组 %s：与当前进程同组，退化为只杀该进程。"
        "请检查子进程是否漏了 start_new_session",
        pgid,
    )
    with contextlib.suppress(OSError, ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    return

with contextlib.suppress(OSError, ProcessLookupError):
    os.killpg(pgid, signal.SIGKILL)
```

为什么明知道第一层已经修好了还要加第二层？因为**这个 bug 的失败模式是"杀死自己"**——一旦回归，代价是整个服务消失，而且几乎无法从现象反推原因。这类缺陷值得付一次冗余判断的成本。

**第三层：回归用例**

```python
@pytest.mark.skipif(sys.platform == "win32", reason="进程组是 POSIX 概念")
async def test_child_runs_in_its_own_process_group():
    code = "import os; print(os.getpgid(0), os.getpgid(os.getppid()))"
    run = await run_crawler_subprocess([sys.executable, "-c", code], cwd=".", timeout=60)
    child_pgid, parent_pgid = run.stdout.split()
    assert child_pgid != parent_pgid
```

这条用例在 Windows 上会 skip，**本地无法验证，只能靠 CI**。这是有意接受的代价：有些缺陷的验证成本注定要放在流水线上。

### 备选方案对比

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| `start_new_session=True` | 一行参数，无依赖，语义精确 | 仅 POSIX | **采用** |
| `preexec_fn=os.setsid` | 效果等价 | Python 文档明确警告：多线程下 `preexec_fn` 不安全（fork 后只能调用 async-signal-safe 函数），uvicorn 是多线程的 | 否 |
| 遍历 `/proc` 递归找子孙逐个 kill | 可移植 | 代码量大、有竞态窗口（进程可能在遍历中 fork） | 否 |
| `psutil.Process.children(recursive=True)` | 最稳、跨平台 | 引入第三方运行时依赖 | 否 |

注意这里**不能**简单地改成 `process.kill()`：MediaCrawler 会拉起 Chromium，只杀父进程会留下孤儿浏览器进程——这正是当初要用 `killpg` 的原因（SPEC 4.8）。

---

## 两次事故的共同点

1. **都是"本地能过、CI 才暴露"**。Windows 开发 + Linux 部署，这个组合天然存在盲区。
2. **现象都具有误导性**。第一次看起来像"平台差异"，实际是 linter 把修复改了回去；第二次看起来像"测试慢"，实际是进程被杀。
3. **都不是业务逻辑错误**，而是工程环境与工具链的问题——恰恰是 CI 最该负责的部分。

---

## 为此做的改动

| 改动 | 作用 |
|---|---|
| `mypy` 增加 `platform = "linux"` | 本地即按部署目标检查，平台差异不再等 CI 发现 |
| 新增 POSIX 进程组回归用例 | 锁死 `start_new_session` 语义 |
| `_kill_process_tree` 同组比对 | 万一回归，退化为单进程 kill 而不是自杀 |
| 全仓 `ruff format` + 严格 lint | 统一风格；也正因为开了 B009 才踩到事故一的坑 |
| CI 增加 `Docker build` + 健康检查 | 保证镜像始终可交付，而不只是"能跑测试" |

---

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 09:25 | push `af15304`，CI #1 启动 |
| 09:27 | Lint ✅ / Type check ❌（`os.startfile`）/ Docker build ✅ |
| 09:27–09:37 | Test matrix 无任何进展，job 卡死 |
| 09:32 | push `2003184`（修 mypy），CI #2 排队 |
| 09:38 | push `a586d29`（修进程组），CI #3 排队 |
| ~09:44 | 手动强制取消 #1、#2 |
| 09:44–09:50 | CI #3 执行 |
| 09:50 | **CI #3 全绿，6m15s** |

---

## 如果重来一次

- **`type: ignore` 不要为了让本地安静就删掉。** 正确做法是按平台配置 mypy，而不是绕开检查。
- **看到 lint 的自动修复，要 review diff。** `--fix` 之后 "All checks passed" 只说明规则满意了，不说明语义没变。
- **CI 里"卡住"比"失败"信息量更大。** 失败会告诉你哪里错；卡住说明执行者已经无法汇报——优先怀疑进程被杀或等待 I/O。
- **跨平台项目要主动构造 Linux 验证路径。** 本地跑不了的分支，必须有 CI 用例覆盖，否则等于没写。
