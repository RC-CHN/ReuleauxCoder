# ReuleauxCoder × Crush 对照改进清单与 LSP 时序设计

> 日期：2026-08-11  
> 状态：调研、设计与实施跟踪（更新于 2026-08-12）
> Crush 基线：`references/crush`，commit `feb63006e9452be370721c22a0c2a3be008fd475`（2026-08-10 nightly）  
> 目的：记录从 Crush 源码中可吸收的产品与运行时改进，并重点校正 ReuleauxCoder 当前 LSP 的启动、同步、诊断、重启和关闭时序。

本文是当前实现的增量审计，不替代 `references/reuleauxcoder-subagent-lsp-handoff.md`。后者记录了 v0.4.0 之前的 stale、generation、workspace scope 等问题；本文以现有代码为准，关注仍未解决或新暴露的时序问题。

## 0. 总体判断

Crush 的强项是 provider 产品化、终端 UI、共享会话后端和较轻的本地工具实现；ReuleauxCoder 的强项是统一工具安全边界、可回放持久化、远端执行一致性、typed runtime events 和异步 subagent。

本轮不应以“改成 Crush 架构”为目标。推荐原则是：

1. 保留 ReuleauxCoder 的 `ToolExecutor`、`ToolOutcome`、Workspace/Process Port、history ledger、diagnostic route/generation 和 artifact 可恢复能力。
2. 吸收 Crush 的懒启动、显式运行状态、有界事件背压、UI 缓存、provider catalog 和本地统计投影。
3. 先修确定性的 LSP 时序错误，再扩充 rename、call hierarchy 等 LSP 功能。
4. 不增加新的万能抽象；优先让已有 provider、extension、event 和 performance 边界真正落地。
5. 让业务、传输、协议和同步故障真实发生，并把安全、结构化的原因交给 agent；只隔离会导致进程或共享运行时崩溃的二次观测、回调和清理故障。隔离时也必须记录错误类型，禁止静默吞掉、伪造成功或伪造 clean 状态。

## 1. 改进登记表

| ID | 优先级 | 改进 | 当前问题 | 完成定义 |
| --- | --- | --- | --- | --- |
| LSP-01 | P0 | 原子化编辑后同步 | `didSave` 与 diagnostics 分属两个队列，实际顺序与源码调用顺序相反 | 一次 `DocumentCommitted` 工作项内完成 sync → didSave → wait/pull → batch |
| LSP-02 | P0 | 迟到诊断 carry-forward | batch 精确绑定原 turn；错过下一次同 turn 请求后可能永久成为孤儿 | 下一安全请求可消费同 agent/session generation 的迟到 batch；有 TTL 和容量上限 |
| LSP-03 | P0 | 冷启动、取消和关闭 deadline 对齐 | 外层工具 10 秒超时，内部 initialize 可 30 秒，worker 仍继续占用；shutdown join 也覆盖不了真实上限 | end-to-end deadline 明确；取消可中断/脱离 waiter；退出不遗留 worker/process |
| EVENT-01 | P0 | 有界事件投递 | TUI 使用无界 `SimpleQueue`，仅 drain 时合并 stream delta | transient 可合并/覆盖；terminal/control 有界必达；暴露 high-water/drop/coalesce 指标 |
| LSP-04 | P1 | 懒 availability 与真实状态 | 启动时全量 `which`，并把“launcher 存在”显示为“server ready” | `unstarted/resolving/starting/ready/error` 状态准确；缺失命令负缓存后可重试 |
| LSP-05 | P1 | 消除跨 transport 队头阻塞 | 所有语言和 workspace root 共用一个串行 worker；一次 5 秒诊断等待会阻塞其他 server | 保持每 transport 单写者，同时允许不同 transport 并行；诊断等待不占住全局调度器 |
| LSP-06 | P1 | Hook 后重新预算 token | LSP diagnostics 在 `BEFORE_LLM_REQUEST` 注入，但本地 request token 估算发生在注入前 | 最终 wire payload 构造完成后再做上下文预算或记录 hook delta |
| OBS-01 | P1 | LSP 与队列性能面板 | 无 queue wait、spawn、initialize、sync、diagnostics wait、late completion、stderr 观测 | `/debug performance` 可按 phase/status 查看；保留有界 server stderr tail |
| PROVIDER-01 | P1 | 真正的 provider port | 实际仍由 OpenAI-compatible `LLM` 具体类承载所有兼容逻辑 | Agent 依赖更新后的协议；至少一个 native provider adapter 通过 contract tests |
| TUI-01 | P1 | 异步缓存 single-flight | 已有虚拟 transcript，但缺少 cache hit/miss、generation 和异步探测 single-flight | resize 分批预热；旧 generation 结果不可污染当前视图；指标可见 |
| SESSION-01 | P2 | 可重建查询投影 | session inventory/stats 依赖目录扫描，长期 session/file 数量上升后扩展性较差 | 增加 SQLite 或等价投影；`events.jsonl/replay` 仍是权威数据 |
| MCP-01 | P2 | generation/renewal 状态机 | 已有后台发现和 catalog seal，但 reconnect/capability 变化的状态表达仍可加强 | per-runtime generation、single-flight renew、动态 capability 事件和耗时可见 |
| CORE-01 | P2 | 收敛三重事件和大类 | `AgentEvent → RuntimeEvent → UIEvent` 兼容层较多；多个 manager/loop 已成为大类 | 删除不再需要的 alias/bridge；subscriber 故障可观测；职责按稳定边界拆分 |

## 2. LSP 必须保持的正确性边界

在讨论调度方式前，先固定以下不变量：

1. Transport 身份是 `(language, resolved_workspace_root, transport_generation)`，不能只按 server name 或 language。
2. Document 身份使用 canonical absolute path/URI；同一逻辑不得混用调用参数中的相对路径。
3. 文档版本必须单调递增；诊断必须关联 document version 或本地 diagnostic generation。
4. `publishDiagnostics` 是目标 URI 的完整替换；空列表代表明确清除，不是“没有结果”。
5. 编辑后的诊断只能路由到正确的 agent、session generation 和 workspace；origin turn/tool 作为 provenance，不应让迟到结果永久失去消费机会。
6. 模型最多消费一个 batch 一次；消费应在成功写入 wire payload 后 ack，而不是在渲染前删除。
7. Remote workspace 没有一致文件视图时，Host LSP 不得读取本地同名文件冒充远端诊断。
8. 用户取消一个工具调用可以停止等待，但不能把共享 LSP transport 留在半初始化状态。

## 3. Crush 的 LSP 实际时序

### 3.1 应用启动只登记，不启动 server

Crush 的 `app.New` 创建 `lsp.Manager`，`NewManager` 只加载 Powernap 默认 server catalog 并合并用户配置，不会 spawn 子进程：

- `references/crush/internal/app/app.go:97-117`
- `references/crush/internal/lsp/manager.go:38-72`

Agent 完成装配后，App 才安装 LSP callback，并异步调用 `TrackConfigured`。它只把用户显式配置的 server 发布为 `unstarted`：

- `references/crush/internal/app/app.go:170-186`
- `references/crush/internal/lsp/manager.go:86-100`

因此 Crush 冷启动没有 PATH 全量扫描和 LSP 子进程启动。恢复 session、文件读取、编辑或显式 LSP tool 才触发真正启动。

### 3.2 首次文件触发的 lazy start

```text
touch/read/edit file
  -> Manager.Start(path)
  -> path 绝对化 + workspace 边界检查
  -> 并发检查所有注册 server
       -> file type
       -> root marker
       -> missing-command 30s negative cache
       -> PATH lookup
  -> New Client
  -> StateStarting
  -> register handlers
  -> initialize (默认 30s deadline)
  -> open root-marker config files
  -> WaitForServerReady
  -> StateReady/Error
  -> callback 更新 UI state
```

对应代码：

- `references/crush/internal/lsp/manager.go:102-119`
- `references/crush/internal/lsp/manager.go:153-250`
- `references/crush/internal/lsp/manager.go:253-304`
- `references/crush/internal/lsp/client.go:104-137`

值得借鉴的细节：

- filetype/root marker 的廉价过滤在 PATH lookup 之前。
- 缺失命令负缓存 30 秒，而不是整个 session 永久不可用。
- `window/workDoneProgress/create` 等 server request handler 在 initialize 前注册，避免 TypeScript server 在握手阶段因无人响应而失败。
- 用户可以先看到 configured-but-unstarted 状态。

但 `Manager.Start(ctx, path)` 是同步等待的。它没有把 caller 的 `ctx` 传给初始化，而是使用独立的 `context.Background()` + 30 秒 timeout。因此 caller 取消不能中断 cold start；首个 view/edit/LSP tool 仍可能承担完整冷启延迟。UI 通过 `tea.Cmd` 调用时不会冻结 Bubble Tea 主循环，但工具调用和 HTTP 调用仍会阻塞。

### 3.3 View 时序

```text
read file from disk
  -> Manager.Start               # cold path 最长约 30s
  -> OpenFileOnDemand
       -> didOpen(version=1)
  -> WaitForDiagnostics(300ms)
  -> read current diagnostic cache
  -> return tool result
```

代码：

- `references/crush/internal/agent/tools/view.go:230-258`
- `references/crush/internal/agent/tools/diagnostics.go:39-83`
- `references/crush/internal/lsp/client.go:401-429`

注释称 view 的诊断等待应保持低延迟，但 300ms 只约束 diagnostics wait，不约束之前的同步 cold start。

### 3.4 Edit/Write 时序

Crush 先把文件写入磁盘，再同步执行：

```text
file committed
  -> Manager.Start
  -> OpenFileOnDemand
       -> 若此前未打开：didOpen(v1, 已是新内容)
  -> NotifyChange
       -> 再读同一份新内容
       -> didChange(v2, whole document)
  -> WaitForDiagnostics
       -> 首次变化最多等约 1s
       -> 变化后等待 300ms quiet period
       -> hard limit 5s
  -> append cached diagnostics to tool result
```

代码：

- `references/crush/internal/agent/tools/edit.go:88-102`
- `references/crush/internal/agent/tools/write.go:137-170`
- `references/crush/internal/agent/tools/diagnostics.go:86-128`
- `references/crush/internal/lsp/client.go:432-462`
- `references/crush/internal/lsp/client.go:620-689`

Crush 没有发送 `textDocument/didSave`。首次编辑一个未打开文件时，还会把相同的新内容先 didOpen(v1)，再 didChange(v2)。依赖 didSave 才触发分析的 server 可能无法及时更新。

### 3.5 Diagnostics 的范围

Crush 的 `publishDiagnostics` 会按 URI 替换缓存，空列表能够清除旧错误，这是正确的：

- `references/crush/internal/lsp/handlers.go:113-132`

但是 `WaitForDiagnostics` 观察的是整个 Client diagnostics map 的 version，而不是目标 URI/document version。结果是：

- 另一个文件的 publish 可以让目标文件提前结束等待。
- publish 如果在 `NotifyChange` 返回后、baseline 采样前到达，会被当作旧 baseline，额外等待到 first-change timeout。
- handler 忽略 server 提供的 document version，迟到的旧诊断可能覆盖新结果。

Crush 的 UI 侧则做得较好：diagnostic count 有 version cache，异步 state fetch 使用 single-flight + dirty bit，并有 TTL 兜底，不在渲染线程做重查询。

### 3.6 Crush LSP 中不应复制的问题

以下是静态源码可确认的问题，记录在此用于避免照搬：

1. `write` 已计算规范化 `filePath`，但通知 LSP 时仍传原始 `params.FilePath`：`references/crush/internal/agent/tools/write.go:66,166`。相对路径可能通过 Manager 启动 server，却在后续 `client.HandlesFile(relativePath)` 处被跳过。
2. symbol helper 对工作目录调用 `Start`：`references/crush/internal/agent/tools/lsp_helpers.go:52-89`。Manager 依赖文件扩展名选择 server，所以全新 session 中 definition/rename/reference 可能无法触发 cold start。
3. restart 保存的是 `file://` URI，随后把 URI 当 filesystem path 传给 `OpenFile`：`references/crush/internal/lsp/client.go:246-301`。原 open documents 实际可能无法重开。
4. restart 只清 diagnostic count cache，没有清 diagnostics map；旧错误可能在新 server 启动后继续显示。
5. `workspace/applyEdit` 直接写文件，绕过 approval、history、filetracker 和统一工具管线：`references/crush/internal/lsp/handlers.go:57-72`。
6. Manager 的并发 Start 是 check-then-set，不是原子 single-flight；并发触发可能启动重复 client。
7. LSP state/broker 是进程全局并按 server name 索引：`references/crush/internal/app/lsp_events.go:39-42`，多 workspace 可能互相覆盖。
8. 正常应用退出走并发 KillAll，而不是 graceful shutdown；速度快，但会跳过 didClose/shutdown/exit。

## 4. ReuleauxCoder 当前 LSP 时序

### 4.1 启动阶段

```text
AppRunner._init_lsp
  -> LspManager(...)
  -> health_check: 遍历全部 language，shutil.which(command)
  -> UI 输出 "language servers ready"
  -> start_worker（此时不启动 server process）
  -> manager 绑定 Agent、Hooks、LspTool
```

代码：

- `reuleauxcoder/interfaces/entrypoint/runner.py:328-394`
- `reuleauxcoder/extensions/lsp/manager.py:186-227`

这里的 “ready” 实际只表示 launcher 存在。Python、TypeScript、JavaScript、Bash、YAML 默认可能使用带 `-y` 的 `npx`；PATH 有 npx 不代表 package 已安装或 initialize 能成功，首次使用还可能发生网络下载。

如果启动时 `report.available == 0`，AppRunner 会直接丢弃 Manager。之后即使用户安装 server，也无法在本 session 重试。这一点不如 Crush 的 30 秒 negative cache。

### 4.2 主动 LSP tool

```text
LspTool.execute
  -> send_request_sync(timeout=10s)
  -> tool queue
  -> worker: get/create transport
       -> spawn
       -> initialize(timeout=30s)
  -> stale check by mtime
       -> didOpen 或 didChange
       -> 若 server 支持 pull diagnostics，同步 pull
  -> actual LSP request(timeout=10s)
  -> Future result
```

代码：

- `reuleauxcoder/extensions/tools/builtin/lsp.py:101-177`
- `reuleauxcoder/extensions/lsp/manager.py:428-472`
- `reuleauxcoder/extensions/lsp/manager.py:529-582`
- `reuleauxcoder/extensions/lsp/manager.py:752-845`

当前外层 `future.result` 只等 10 秒，但 cold initialize 本身可等 30 秒，之后实际 request 还能再等 10 秒。于是调用方可能已经报告 timeout，worker 却继续占用 30～40 秒。`SPAWN_TIMEOUT = 30` 目前只是未使用的常量。

### 4.3 编辑后实际顺序：确定性 P0 问题

源码调用顺序看起来是：

```text
successful edit/write
  -> notify_did_save(path)
  -> enqueue_diagnostics(path, exact route)
  -> poll exact batch for up to 2.5s
```

见：

- `reuleauxcoder/domain/hooks/builtin/lsp_edit_observer.py:94-133`

但两个操作进入不同队列：

- didSave 进入 notification queue：`reuleauxcoder/extensions/lsp/manager.py:474-485`
- diagnostics 进入 diagnostics queue：`reuleauxcoder/extensions/lsp/manager.py:272-334`

worker 的固定优先级是：

```text
tool > diagnostics > notification
```

见 `reuleauxcoder/extensions/lsp/manager.py:518-527`。因此真实时序是：

```text
Hook thread                         LSP worker
-----------                         ----------
enqueue didSave  ----------------> notification queue
enqueue diagnostics -------------> diagnostics queue
poll batch                         pop diagnostics first
                                   capture baseline generation
                                   didOpen/didChange if stale
                                   wait diagnostics, default up to 5s
                                   finish/timeout diagnostics request
                                   pop didSave last
                                   didSave / optional pull diagnostics
```

如果 server 只在 didSave 后 publish：

1. diagnostics request 先等待并 timeout，不产生 batch；
2. 随后 didSave 才让 client buffer/generation 更新；
3. 此时已经没有对应 Manager request 把结果封装成 routed batch；
4. 下一次 edit 又会重复同样顺序，诊断可能持续无法注入模型。

现有 `tests/extensions/lsp/test_manager.py:426-444` 还明确固化了 diagnostics 高于 notification 的顺序，因此需要同时修改测试语义。

最低成本且正确的修法不是简单交换队列优先级，而是删除这条路径上的独立 didSave notification，把一次成功文件提交表示成单个原子工作项：

```text
DocumentCommitted
  -> ensure transport
  -> canonical content sync: didOpen 或 didChange
  -> didSave
  -> pull diagnostics 或等待目标 URI 的新 publish
  -> produce DiagnosticBatch
```

### 4.4 单 worker 的跨 server 队头阻塞

Transport 已正确按 `(language, workspace_root)` 分开，但所有 transport 的工作仍由同一个 async worker 串行 await：

- `reuleauxcoder/extensions/lsp/manager.py:119-125`
- `reuleauxcoder/extensions/lsp/manager.py:489-516`

一次 cold initialize、5 秒 diagnostics wait 或慢 request 会阻塞其他语言和 workspace root。连续 tool request 还可能长期饿死 diagnostics/notification。

短期应先修原子 commit 和 deadline；随后可以采用“一个 event-loop thread、每 transport 一个 actor/lock”的方式：保持同一 server 的单写者，同时让不同 transport 并行，并让等待 diagnostics 的 task 不占住全局 dispatcher。

### 4.5 已做对的 diagnostic generation/route

以下设计应保留：

- document version 单调递增；
- push diagnostics 拒绝明确旧 version；
- empty publish 代表显式 clean；
- pull diagnostics 支持 result ID；
- request sequence 防止慢旧请求覆盖新 batch；
- route 包含 agent/session generation/session/turn/tool/file；
- batch 的消费与 ack 在锁内原子完成；
- reset 推进 generation 并淘汰旧 batch；
- runtime diagnostic event 保留 correlation。

对应代码：

- `reuleauxcoder/extensions/lsp/client.py:171-266`
- `reuleauxcoder/extensions/lsp/client.py:489-548`
- `reuleauxcoder/extensions/lsp/manager.py:274-424`
- `reuleauxcoder/extensions/lsp/manager.py:584-730`

### 4.6 迟到 batch 的跨 turn 孤儿问题

Edit hook 创建的 route 固化当前 `turn_id/tool_call_id`。Injector 又只消费当前请求完全相同的 agent、session generation、session 和 turn：

- `reuleauxcoder/domain/hooks/builtin/lsp_edit_observer.py:109-117`
- `reuleauxcoder/domain/hooks/builtin/lsp_injector.py:79-97`

Hook 最多同步等 2.5 秒，但 Manager diagnostics wait 默认可到 5 秒。典型失败序列是：

1. Edit hook 在 2.5 秒时放弃即时注入；
2. 下一次同 turn LLM request 已发出；
3. batch 在 request 进行期间完成；
4. 当前 LLM 返回最终答案，turn 结束；
5. 下一次用户消息使用新 turn ID，Injector 永远不匹配旧 batch。

建议：

- ownership 隔离使用 `agent_id + session_generation + session_id`；
- `origin_turn_id + origin_tool_call_id` 保留为 provenance；
- 迟到 batch 允许在该 session generation 的“下一安全请求”carry-forward；
- pending batch 设置 TTL、每 agent 容量上限和 overwritten/stale 计数；
- Injector 使用 `peek → render/inject → ack`，避免注入失败后 batch 已被删除。

### 4.7 Token 预算顺序

AgentLoop 在调用 `LLM.chat` 之前已经计算 local request estimate；LSP Injector 则在 `LLM.chat` 内部的 `BEFORE_LLM_REQUEST` hook 才修改消息。因此模型实际收到的 diagnostics 不进入之前的本地预算。

建议把最终预算边界固定为：

```text
build stable history
  -> add volatile execution tail
  -> run before-LLM transforms
  -> freeze exact wire payload
  -> estimate/calibrate final payload
  -> context limit decision
  -> dispatch
```

所有能修改 wire payload 的 hook 都应遵守这一边界，不只 LSP。

### 4.8 Shutdown 时序

AppRunner 先解绑 Hook/Tool，再调用 `manager.shutdown_all()`，方向正确：

- `reuleauxcoder/interfaces/entrypoint/runner.py:652-663`

Manager 设置 stop/abort，清空 queued work，worker finally 再逐个 shutdown client：

- `reuleauxcoder/extensions/lsp/manager.py:229-270`
- `reuleauxcoder/extensions/lsp/manager.py:489-516`
- `reuleauxcoder/extensions/lsp/manager.py:861-870`

问题在于：

- in-flight initialize 最长 30 秒，`join` 只等 15 秒；
- `_abort_current` 不会取消正在 await 的 spawn/initialize；
- 多个 client 是串行 shutdown；单 client 最坏可花约 10 秒；
- join timeout 后 daemon worker 仍可能存活，但上层 cleanup 已继续并报告完成。

KISS 修法：保存 current asyncio task，shutdown 时用 worker loop 安全地 cancel；先 terminate/kill process 解除 stdio wait，再用 `gather` 并发收尾。若产品选择退出速度优先，也应明确采用有界 force-kill，而不是报告“已关闭”后仍让 daemon worker 存活。

## 5. 推荐的目标 LSP 时序

### 5.1 状态模型

```text
TransportState
  unstarted
  resolving        # command/root/capability resolution
  starting         # process created
  initializing     # LSP initialize handshake
  ready
  degraded         # process alive, capability/diagnostic path异常
  stopping
  stopped
  error
```

每次 start/restart 增加 `transport_generation`。来自旧 process reader 的 response、diagnostics 和状态事件必须携带 generation，并在进入 current state 前校验。

### 5.2 Startup

```text
load declarative LSP catalog
  -> register configured servers as unstarted/unknown
  -> do not scan every command
  -> do not start process
```

首次遇到支持文件时：

```text
detect language
  -> resolve nearest workspace root
  -> cheap root/file checks
  -> availability lookup
       launcher found != server ready
       missing command negative-cache 30s
  -> per-transport single-flight start
  -> emit phase timings and final state
```

### 5.3 Read-only LSP query

```text
canonicalize file + capture deadline
  -> ensure transport
  -> sync document if revision changed
  -> send query
  -> return result
```

Definition/reference/symbol 查询不应隐式等待 diagnostics。需要诊断时使用明确的 diagnostics request。

### 5.4 Successful edit/write

```text
ToolExecutor commits mutation
  -> ToolOutcome contains canonical path + content revision/hash
  -> enqueue one DocumentCommitted(route, revision, deadline)
  -> per-transport actor:
       ensure transport
       baseline = diagnostics_generation(uri)
       didOpen(v1) OR didChange(vN)
       didSave(vN)
       if pull supported:
           pull current document diagnostics
       else:
           wait until target URI generation > baseline
           and, when supplied, published version >= document version
       publish DiagnosticBatch(status, route, version, generation)
  -> edit hook waits a small immediate-feedback budget
  -> otherwise next safe model request carries batch forward
```

Batch status至少区分：

- `published_nonempty`
- `published_clean`
- `timed_out`
- `server_unavailable`
- `stale_discarded`
- `cancelled`

Timeout 不是 clean publish，不能清除旧错误。

### 5.5 Deadline 所有权

应区分三个 deadline：

1. `start_deadline`：共享 transport single-flight 初始化上限。
2. `operation_deadline`：当前 tool/query 的 end-to-end 上限，包含 queue wait。
3. `diagnostics_feedback_budget`：为了本轮即时反馈最多等待多久；超时后 batch 仍可后台完成并 carry-forward。

调用方取消时可以停止等待共享 start，但共享 start 是否继续由 manager policy 决定；无论哪种选择都必须有明确状态和事件，不能形成“调用方已失败、worker 无声占用 30 秒”的状态。

## 6. LSP 可观测性要求

### 6.1 状态事件

每个 transport 至少发布：

- state transition + generation
- language + workspace root hash
- command/launcher 名称（不含 secret/env）
- queue depth / queue wait
- spawn、initialize、sync、request、diagnostics wait、shutdown elapsed
- error type + bounded stderr tail reference
- respawn count

不要再把 `shutil.which(npx)` 显示为 “language server ready”。建议文案使用：

- `configured`
- `launcher found`
- `starting`
- `ready`
- `unavailable`
- `initialization failed`

### 6.2 性能样本

接入现有 `RuntimePerformanceMonitor`：

```text
category=lsp
name=availability_lookup | queue_wait | spawn | initialize | document_sync |
     request | diagnostics_wait | shutdown | total
attributes=
  language, root_hash, method, transport_generation,
  document_version, diagnostic_generation, status
```

另需计数：

- cold starts
- negative-cache hit/miss
- coalesced document commits
- stale diagnostics discarded
- diagnostic batch carried forward
- diagnostic batch expired/overwritten
- late completion after caller timeout
- worker HOL 超过阈值次数
- force-killed servers

### 6.3 stderr

当前 `LspClient.spawn` 将 server stderr 直接指向 `DEVNULL`。建议保留固定上限，例如 64 KiB ring buffer；成功时无需展示，initialize/crash 失败时在 debug view 中显示末尾若干行，并确保不进入模型上下文。

## 7. 实施顺序

### Phase A：先修 correctness

1. 引入单一 `DocumentCommitted`/`DiagnosticRequest`，删除 edit path 的独立 didSave notification。
2. 在同一工作项中固定 sync → didSave → target-URI wait/pull。
3. 迟到 batch 改为 session-generation carry-forward；加入 TTL/容量。
4. Injector 改成成功注入后 ack。
5. 最终 wire payload 在 hook 后重新做 token budget。

### Phase B：修 lifecycle 和性能

1. eager health check 改为 lazy availability + negative TTL。
2. 引入真实 transport state、generation 和 start single-flight。
3. 对齐 start/operation/feedback deadline；取消能够终止或脱离 waiter。
4. 解决单 worker HOL；至少让 diagnostics wait 不阻塞其他 transport。
5. shutdown 能取消 in-flight init，并在 deadline 内完成或明确 force-kill。

### Phase C：补产品能力

1. 增加 `lsp_status`/`lsp_diagnostics`/`lsp_restart`，先解决运维和诊断能力。
2. 再考虑 rename、call hierarchy、replace symbol。
3. session restore 可只对最近读写文件做有预算的后台预热；不得在恢复关键路径同步启动全部 server。
4. TUI 展示 configured/starting/ready/error、诊断 count 和 slow phase。

## 8. 必补测试

### 8.1 确定性时序

- save-only publish server：必须收到 didOpen/didChange → didSave 后的新 diagnostics。
- push-on-change server：不得重复或丢失 diagnostics。
- pull diagnostics server：full、unchanged、clean 都产生正确 batch。
- 同文件快速连续两次 commit：旧 request 不得覆盖新 batch。
- didSave 与 diagnostics 不再依赖跨队列优先级。

### 8.2 Route/carry-forward

- batch 在 2.5 秒之后、当前 turn 结束前后到达，都能在下一安全请求注入。
- reset/new/restore 后旧 generation batch 不得注入。
- batch 注入失败时不 ack；重试成功后只 ack 一次。
- TTL、容量和 overwrite 有确定性计数。
- parent/subagent 不串线；明确 LspTool 是否允许 subagent 共享只读 transport。

### 8.3 Lifecycle/deadline

- cold initialize 20 秒、tool timeout 10 秒时，不产生失控的 late future/worker 占用。
- caller cancel、server crash、initialize hang、stdout EOF 都有终态。
- 同一 transport 并发首次请求只 spawn 一个 process。
- 不同 transport 的慢 diagnostics 不阻塞快速 tool query。
- shutdown during initialize 能在声明 deadline 内结束。
- 多 client shutdown 不因串行 10 秒上限超出总 join deadline。

### 8.4 Availability/observability

- PATH 缺失进入 negative cache，TTL 后能重试。
- `npx` 存在但 package/init 失败时绝不显示 ready。
- state transition 顺序和 generation 正确。
- stderr tail 有界且不包含环境变量/credential。
- performance samples 覆盖 queue wait、spawn、init、sync、request、diagnostics wait 和 shutdown。

## 9. LSP 之外的具体吸收项

### 9.1 事件背压

Crush 的 pubsub 区分普通 lossy event 与 bounded must-deliver terminal event，并记录两类 drop count：

- `references/crush/internal/pubsub/broker.go:159-230`

ReuleauxCoder 应把这一语义放在 RuntimeEvent/UI adapter 边界：stream delta 可以按 correlation key 合并；tool finish、approval、cancel、error 等必须有界必达。不要直接复制 Crush 的固定 4096/50ms 数字，应基于我们的 UI paint/remote relay 延迟测试确定。

### 9.2 Provider

Crush 的 Fantasy/Catwalk provider 面是最大的产品差距。优先让现有 `LLMProtocol` 与真实 `LLM.chat` 签名对齐，并让 Agent 依赖协议；随后增加 native adapter、auth、capability 和 cost metadata。不要把更多 provider special-case 继续堆入单一 `LLM.chat`。

### 9.3 Session 查询投影

可以增加可重建的 SQLite inventory/stats 投影，支持 session、token、cost、tool usage 和响应时间查询。它不应替代：

- append-only `events.jsonl`
- canonical `replay.json`
- immutable request envelope
- checkpoints/artifacts

### 9.4 TUI 缓存

在现有 virtual transcript 上补：

- generation-based stale result rejection
- single-flight async probe
- resize 后分批 prewarm
- cache hit/miss、render rows、layout elapsed

不要复制 Crush 的 5000 行顶层 UI model 或依赖所有 mutator 手工 bump version 的隐式契约。

### 9.5 MCP

借鉴 Crush 的 generation、dirty reconcile、renewal single-flight、动态 capability 和 process-group cleanup；保持 ReuleauxCoder 的 per-runtime/per-workspace ownership，不改成 package-global registry。

## 10. 明确不做

1. 不自动执行仓库内 shell 配置作为启动配置。
2. 不采用基于命令前缀的“安全 Bash”免审批。
3. 不让 LSP `workspace/applyEdit` 绕过 ToolExecutor/approval/history。
4. 不把 MCP/LSP 改成进程全局单例。
5. 不把 tool output 截断后丢弃原文。
6. 不用 SQLite 替换现有 ledger/replay 权威记录。
7. 不采用 hook 失败后继续执行 effectful tool 的 fail-open 语义。
8. 不为追求功能数量先扩 LSP tool；正确的 sync/diagnostics/lifecycle 时序优先。

## 11. 本轮结论

最先应动的不是 LSP 功能表，而是 `LSP-01`：把编辑后的 document sync、didSave 和 diagnostics 变成同一原子工作项。当前分队列设计存在可确定复现的时序反转；只改优先级不能彻底解决。

第二步是 `LSP-02/LSP-03`：让迟到 batch 能 carry-forward，并让 cold start、调用超时和 shutdown 使用一致、可观测的 deadline。完成这三项后，再做 lazy availability、per-transport concurrency、状态 UI 和新工具，收益才不会建立在不稳定时序上。

## 12. 2026-08-12 实施记录

本轮完成了 LSP 之外的三组改进，每组保持独立提交：

| ID | 状态 | Commit | 已落地边界 | 验证 |
| --- | --- | --- | --- | --- |
| `PROVIDER-01` | 完成 | `542c80e` | Agent 依赖 provider-neutral `LLMProtocol`；增加原生 Anthropic Messages/SSE adapter、配置贯通、tool/usage/thinking stream 转换、安全传输错误和 contract tests | 全量 `1995 passed, 25 skipped` |
| `TUI-01` | 完成 | `dc2f29d` | transcript resize 使用单 worker single-flight 分批预热；generation stale rejection；cache hit/miss、batch、render rows、elapsed 进入 `tui_cache` performance samples；后台失败进入 UI/runtime incident | 全量 `2000 passed, 25 skipped`；1000 cells resize 调度约 1.5 ms，后台完成约 383 ms |
| `SESSION-01` | 完成 | `a6b5c76` | 增加可丢弃、可重建 SQLite inventory projection；dirty marker 覆盖 save 崩溃窗口；候选 manifest freshness 校验；索引损坏自动重建；token/event/request/checkpoint 聚合查询 | 全量 `2005 passed, 25 skipped`；300 sessions 查询由首次扫描约 68 ms 降至约 0.9 ms |
| `LSP-TOOLS` | 完成 | `97db356`, `70e743d` | 增加显式 `lsp_diagnostics` 与按文件/transport scope 的 `lsp_restart`；两者复用 per-transport FIFO、generation gate、typed terminal outcome；调用方超时/取消只脱离 waiter，不伪造 clean；restart 必须确认新 generation READY | 全量分别 `2013 passed, 25 skipped`、`2021 passed, 25 skipped`；真实 stdio server 验证 diagnostics、旧/新 PID 与 initialize failure |
| `MCP-01` | 完成 | `2993bf8`, `ed6a61d`, `4ed5d77` | per-manager/per-server runtime slot；generation stale rejection；connect/reconnect single-flight；EOF 不再显示 connected；`tools/list_changed` dirty-bit coalesce；目录原子替换并注入 Agent；刷新/renew/connect/disconnect 耗时可见；POSIX process group/Windows tree cleanup；cleanup 失败返回错误且记录 runtime issue | 全量最终 `2043 passed, 25 skipped`；8-way single-flight、旧 generation callback、burst 最多两次 list、刷新失败无 stale tools、真实 signal-resistant 子进程回收 |
| `CORE-01`（本轮边界） | 完成 | `1c88356`, `ce0224d` | UI subscriber、TUI cache/projection、MCP callback/monitor/cleanup 等二次故障保留 bounded safe fact 并注入下一次 Agent 请求；启动进度回调失败也先缓冲，Agent 建立后归属注入；未做无收益的三层事件大改名 | 全量最终 `2046 passed, 25 skipped`；startup progress 定向测试覆盖前置缓冲、即时注入和 sink 二次失败保留 |

以上改动都遵守同一故障原则：provider/协议/权威 session artifact、MCP capability refresh 和 LSP terminal outcome 的业务失败继续真实发生；只隔离 provider cleanup、TUI prewarm、session projection、UI subscriber、MCP catalog observer 和进程 cleanup 这类会反向击穿共享运行时的次生失败，并把安全、结构化的错误类型暴露给 agent。SQLite 只保存可重建查询字段，`events.jsonl`、`replay.json`、request envelope、checkpoint 和 artifact 仍是权威数据。

本轮没有把 cost、tool usage、response latency 加入 session 聚合，也没有扩展第三个 native provider；这些属于现有投影/provider port 上的后续增量，不是本轮完成定义的一部分。

`CORE-01` 的“删除 bridge/拆大类”没有机械执行。当前 `AgentEventBridge` 仍是 domain event 到 typed `RuntimeEventPayload` 的单一适配点，删除它会把转换职责散回 CLI/TUI/remote consumer；MCP 则已用小型 runtime slot 和 client-owned refresh actor 收敛职责。继续拆分 `AgentLoop`/LSP manager 应以新的可测试边界为触发条件，而不是按行数拆类。
