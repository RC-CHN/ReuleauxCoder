# ReuleauxCoder Subagent / LSP 调研 Handoff

> 调研日期：2026-07-11
> 范围：项目职责划分、subagent 生命周期与结果回注、LSP 初始化/文档同步/诊断注入时序、stale 边界。
> 状态：v0.4.0 所列 Subagent/LSP 边界已修复并验收；正文保留修复前调研基线。

## 0. v0.4.0 实施闭环（2026-07-12）

- Subagent Job 先登记后 submit，显式区分 completed/failed/cancelled/timed_out/stale；绑定 parent agent/session generation，reset/new 后拒绝迟到回注。
- cleanup/shutdown、queued/running cancellation、retention/pruning 与 Shell 子进程终止均有确定性测试；父子 Agent 的 Tool/Hook/LSP scope 隔离。
- LSP 队列逐项处理；document version 单调递增；diagnostics 按 URI/version/generation replace/clear，并通过 `DiagnosticBatch` 按 agent/session/turn/tool/file 路由。
- reset 会推进 LSP generation watermark、清空旧 queued/completed batch，并拒绝 reset 时仍在执行的旧请求；多 root transport 隔离，Remote workspace 明确禁用 Host LSP。
- LSP client 同时支持 LSP 3.17 pull diagnostics、`publishDiagnostics` push、server request response 与 clean result；stale/快速保存/双文件/父子隔离均有单元及真实进程测试。
- TypeScript `auto` 模式优先 TS 7 原生 `tsc --lsp --stdio`，并保留显式 TS 6 + `typescript-language-server` legacy 模式；TS/JS 两条当前链路均已用真实 server 验证，Python 与 Rust 同样纳入真实矩阵。

TypeScript 选型依据使用官方资料：[TypeScript 7.0 发布说明](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/)、[Native Previews 与 LSP 说明](https://devblogs.microsoft.com/typescript/announcing-typescript-native-previews/)、[typescript-go](https://github.com/microsoft/typescript-go)；TS 6 兼容链路参考 [typescript-language-server](https://github.com/typescript-language-server/typescript-language-server)。

## 1. 项目与验证基线

ReuleauxCoder 是一个 Python 实现的终端编程 Agent，提供 OpenAI-compatible LLM、工具审批、Hooks、Skills、MCP、LSP、上下文压缩、会话持久化、subagent，以及 Go peer 远程执行。

当前基线：

- 版本：`v0.3.3`，`main` 与 `origin/main` 对齐。
- Python 源码约 22,201 行，测试约 9,997 行。
- 常规 Python 测试：`536 passed, 18 skipped`。
- Go peer：`go test ./...` 通过，但各包均无 Go 测试文件。
- 常规测试中的 LSP integration smoke 默认跳过。
- 显式启用真实 LSP smoke 后：6 passed、3 failed、1 skipped。
  - Python、YAML、Bash、Go、C、C++ 首次诊断通过。
  - TypeScript、JavaScript initialize 失败：找不到有效 TypeScript installation。
  - Rust 启动后未在测试时限内返回预期诊断。

首次完整测试曾因本机 SOCKS5 代理但未安装 `socksio` 而失败；清除代理并允许本地 socket 后，常规测试全部通过。这属于环境兼容风险，不是上述 subagent/LSP 判断的依据。

## 2. 模块职责现状

| 模块 | 应承担职责 | 当前边界观察 |
|---|---|---|
| `domain/agent` | 对话状态、轮次、工具调用闭环、停止与恢复 | 实际直接依赖 subagent、prompt、platform，承担过重 |
| `domain/context` | Token 估算、压缩、摘要和上下文墙保护 | 相对独立 |
| `domain/hooks` | Hook 协议、顺序、失败语义 | Registry 清楚；内置 Hook 已依赖 LSP、UI、文件系统 |
| `services/llm` | LLM 调用、消息清洗、重试、诊断 | 父 Agent 与 subagent approval judge 可能并发复用同一实例 |
| `services/prompt` | system prompt 的稳定构造 | 职责清楚 |
| `services/config` | 配置加载、合并、验证 | 职责清楚，但配置面较复杂 |
| `extensions/tools` | 工具定义、策略、backend dispatch | Tool 实例存在 `_cwd` 等可变状态，不适合父子 Agent 共享 |
| `extensions/subagent` | 子任务调度、隔离、审批、回注、取消和回收 | 创建/回注较完整；取消、session 隔离、shutdown 不足 |
| `extensions/lsp` | Transport、文档同步、请求调度、诊断版本 | stale、事件丢失和跨 Agent 串线的主要来源 |
| `extensions/remote_exec` | Host/Peer 协议、鉴权和远端工具执行 | Host LSP 与 Peer 文件视图缺少明确边界 |
| `interfaces/entrypoint` | 依赖装配和生命周期 | LSP 注入、模块全局 manager、cleanup 集中于此 |
| `interfaces/cli/tui` | 输入输出与交互 | CLI 较完整，TUI 尚薄；后续需先抽共享展示模型 |

总体问题不是目录数量不足，而是运行态状态的 ownership 不够明确：Agent、subagent、LSP manager 都会读写异步结果，但缺少统一的 `agent_id/session_generation/turn_id/tool_call_id/document_version` 关联。

## 3. Subagent 当前时序

```text
AgentTool
  -> SubagentManager.submit_background / run_sync
  -> 选择 main/sub model profile
  -> 按 explore/execute/verify 过滤父 Agent 工具
  -> 浅拷贝父 HookRegistry
  -> 创建子 Agent
  -> daemon thread 执行 sub.chat(task)
  -> Future callback 更新 SubagentJob
  -> inject_subagent_job_result
  -> 父 Agent 新用户轮次前 drain 作为补偿
```

### 3.1 已经处理得较好的边界

- `explore/execute/verify` 有固定工具白名单。
- 后台执行只允许 `explore`。
- explore 并行度限制为 1～4。
- 子 Agent 消息历史独立。
- 父 Agent 有悬空 tool calls 时，subagent 结果暂存到 `_pending_subagent_injections`，避免 assistant 消息插入 tool call 与 tool result 中间。
- 完成 callback 与下一轮 `drain_completed_for_parent()` 构成双通道回注，并使用 `injected_to_parent` 防重复。

### 3.2 P0/P1 风险

#### P0：Job 登记竞态

`submit_background()` 先提交 Future、安装 callback，最后才写 `_jobs` 和 `_futures`。极快任务可能在登记前完成；callback 发现 `_jobs[job_id]` 不存在后返回，Job 会永久停在 `queued`，结果也不会回注。

建议：先在锁内登记 Job，再 submit；Future 创建失败时回滚。callback 不应依赖“稍后一定登记”。

#### P0：超时只是 detach，不是停止

`run_subagent_task()` 超时后调用 `sub.request_stop()` 并返回，但内部 daemon thread 仍可能卡在 LLM、Shell 或文件写操作。execute-mode 子 Agent 可能在报告 timeout 后继续改变工作区。

建议：建立贯穿 AgentLoop、ToolExecutor、Shell subprocess 和 LLM stream 的 cancellation token；cleanup 时等待或强制终止可终止资源。

#### P0：reset/new session 可接收旧任务结果

`Agent.reset()` 只清空 messages/token/round，没有取消 Job、清空 pending injections 或递增 session generation。旧 callback 可把结果注入新会话。

建议：每个 Job 固化 `parent_agent_id + session_generation`；callback 注入前校验。`reset/new/restore` 应切换 generation，并定义 cancel、detach、archive 策略。

#### P1：父子 Agent 共享 Tool 实例

`_filter_subagent_tools()` 直接返回父 Agent 中的 Tool 对象。父 Agent 与并行 subagent 会共享 `ShellTool._cwd`、backend context 及其他可变状态，可能互相改变 CWD 或远程执行上下文。

建议：ToolRegistry 提供 per-Agent factory；schema/descriptor 可以共享，runtime instance 不共享。

#### P1：Hook 浅拷贝仍共享 LSP manager

`HookRegistry.clone()` 使用 `copy.copy()`。Hook 实例不同，但 `lsp_manager` 仍是同一对象。explore subagent 每次 LLM 请求也可能运行 LSP diagnostics injector，提前 drain 父 Agent 的诊断；execute subagent 还可能把其他 Agent 的诊断附加到自身工具结果。

建议：Hook clone 支持 scope policy：`clone`、`share`、`omit`、`rebind`。subagent 默认 omit LSP consumer 类 Hook；若需要 LSP，必须使用按 Agent 路由的 consumer。

#### P1：线程异常可能被包装为空成功

执行 `sub.chat()` 的内部线程只写 `holder["result"]`，没有捕获异常。线程异常结束后，外层可能返回 completed + empty，而非 failed。

建议：Future/Result 类型显式携带 value/error/cancelled/timed_out，禁止依靠字符串 marker 判定状态。

#### P1：无统一 shutdown 和 Job 回收

`AppRunner.cleanup()` 不关闭 SubagentManager 的线程池，也不等待/取消 Job；Job 字典没有 pruning。退出时后台任务可能继续访问已经关闭的 LSP/MCP/Relay。

建议：SubagentManager 成为 AppRunner 显式依赖，提供 `shutdown(cancel_pending, deadline)`、Job retention 和 session detach 策略。

#### 其他边界

- `_get_parent_approval_lock()` 的 lazy creation 本身未同步，并发首次创建可能拿到不同锁。
- ParentLLMJudge 会在后台使用父 Agent LLM，与父 Agent 下一轮请求并发；需要明确 LLM client 是否允许并发以及 trace/UI 状态是否隔离。
- execute/verify 的 parent-LLM auto-approval 是重要安全策略：同一模型体系可替代人工批准 shell/write，需要产品层明确，而不应只是实现细节。
- `max_timeout_seconds` 构造参数目前没有真正参与 clamp。

## 4. LSP 当前时序

### 4.1 启动

```text
注册 Hooks（manager=None）
  -> 创建 LspManager
  -> health_check
  -> start_worker
  -> 向支持 set_lsp_manager 的 Hooks 注入 manager
  -> agent.lsp_manager = manager
  -> LspTool 模块全局 manager = manager
```

### 4.2 编辑与诊断注入

```text
edit_file/write_file 返回
  -> AFTER_TOOL_EXECUTE LspEditObserver
  -> enqueue didSave
  -> enqueue diagnostics
  -> Observer 最多轮询 2.5 秒
       -> 有结果：附加到 tool result，mark diagnostics_fed
       -> 无结果：期望下一次 BEFORE_LLM_REQUEST injector drain
```

“尽量同轮反馈，慢结果下一轮补偿”的方向合理，但实现中的 batch ownership 不足。

## 5. LSP stale 与串线的确定来源

### P0：worker 同时 pop 三个队列，但只执行一个

worker 每轮同时从 tool、diagnostics、notification 各 pop 一个，之后用 `if/elif` 只执行最高优先级项。其余已 pop 的项永久丢失。Observer 连续 enqueue didSave 和 diagnostics 时，常见结果就是 didSave 被取出但未执行。

建议：先根据优先级选择一个非空队列，再只从该队列 pop；或统一成带 kind/priority/sequence 的单队列。

### P0：didChange 版本固定为 2

`didOpen` 使用 version 1；所有后续 `didChange` 默认都是 version 2。第二次之后的编辑可能被严格 LSP server 视为过期并忽略。

建议：以 `(workspace_root, language, uri)` 维护单调递增 document version。

### P0：publishDiagnostics 被追加而不是替换

LSP 的 `publishDiagnostics` 表示该 URI 的最新完整诊断集合，当前 client 却执行 `existing + items`。空 diagnostics 也无法清理已有 items。

结果包括：修复后旧错误仍出现、重复错误、下一次请求立即消费旧通知。

建议：每次 publish 按 URI 覆盖；保留 notification generation/version 元数据，而不是累加 Diagnostic 列表。

### P0：全局 `_diagnostics_fed` bool 会误删其他批次

Observer 给模型注入 A 后设置全局 bool。B 的诊断随后到达；下一次 injector 先 drain B，再因 bool=True 跳过，B 被直接丢弃。该 flag 不按 Agent、turn、file、tool call 或 batch 隔离。

建议：改为 DiagnosticBatch ID 和消费确认；只有完全相同 batch 才去重。

### P1：clean 结果不会覆盖 manager 中旧结果

Manager 仅在 `if diagnostics:` 时写 `_results`。新版本返回空列表时没有明确的 clean 状态，尚未消费的旧错误可能继续被注入。

### P1：`seq` 参数未使用

`enqueue_diagnostics(path, seq)` 接收轮次序号，但 handler 不使用它，无法判断结果是否落后于最新编辑，也无法路由到发起的 Agent/tool call。

### P1：Observer drain 所有文件

一个文件编辑完成后调用全局 `drain_diagnostics()`，可能把其他文件、其他 subagent 或父 Agent 的结果全部附加到当前 tool result。

### P1：每种语言只有一个 server

Transport key 只有 `LanguageId`，没有 workspace root。多根仓库会复用第一个 root 的 server，项目索引、配置和 import resolution 可能错误。

建议：transport key 使用 `(language, resolved_workspace_root)`。

### P1：远程编辑与 Host LSP 文件视图不一致

Remote backend 在 Peer 写文件，LSP Hook 却在 Host 根据同一字符串路径读取本地文件。Host 可能诊断旧文件、同名文件或不存在文件。

建议：远端工具默认不触发 Host LSP；若需要远端 LSP，应在 Peer 执行或把明确版本的文件快照传回 Host 分析。

### 其他生命周期问题

- health check 只检查默认 command 是否在 PATH，没有按 server override 检查，也不能保证 npx package 能成功 initialize。
- 初次 spawn 失败会把语言标记为本 session unavailable；瞬时失败缺少合理重试。
- LspTool 通过模块全局变量持有 manager，多 AppRunner/测试实例会互相覆盖；一个 runner cleanup 可令另一个 runner 的 Tool 失效。
- cleanup 后 Hook 仍持有已 shutdown manager；该 manager 的 `_abort_current` 也不会在 restart 时重置。
- Observer 无论工具字符串结果是否为 `Error:` 都可能触发 LSP，缺少结构化 ToolOutcome.success。

## 6. 建议状态模型

```text
LspDocumentState
  key = (workspace_root, language, uri)
  version
  content_hash
  latest_requested_generation
  latest_published_generation
  latest_diagnostics

DiagnosticBatch
  batch_id
  source_agent_id
  session_generation
  turn_id
  tool_call_id
  file_uri
  document_version
  diagnostics
  consumed_by
```

核心原则：

1. Producer（LSP transport）只发布结构化、带版本的最新状态。
2. Router 根据 agent/session/tool call 分配 batch。
3. Consumer（即时 tool-result feedback 或下一轮 injector）显式 ack batch。
4. 去重基于 batch/version，不使用全局 bool。
5. reset/restore/cleanup 通过 session generation 隔离迟到结果。

## 7. 推荐修复顺序

1. 修 worker pop 丢事件。
2. 文档 version 单调递增。
3. publishDiagnostics 覆盖语义，空结果可清理。
4. 等待诊断时记录 baseline generation，只接受更新通知。
5. 用 DiagnosticBatch 替换全局 drain + bool。
6. subagent 禁止共享 LSP consumer，Tool runtime instance per Agent。
7. Job 原子登记、异常捕获、session generation 和 shutdown。
8. transport 按 workspace root 隔离。
9. 定义 remote backend 的 LSP 策略。
10. 再处理 UI 展示、去重文案和性能体验。

## 8. 必补测试

### Subagent

- Future 在登记前完成的确定性竞态测试。
- subagent 异常必须成为 failed。
- timeout 后不得继续执行下一工具；Shell 子进程可取消。
- `/reset`、`/new`、session restore 后旧 callback 不得注入。
- 父 Agent 与多个 subagent 的 Shell CWD 不串线。
- cleanup 会关闭 pool、处理 queued/running jobs。
- Hook clone scope：LSP consumer 不被 subagent 继承。

### LSP 单元/并发测试

- tool/diag/notif 同时排队均不会丢。
- 连续 didChange version 为 2、3、4。
- diagnostics `[error] -> []` 能清掉错误。
- publish 多次按最新集合替换，不累加。
- 等待新诊断不会立即消费上一次 buffer。
- A 已即时注入时，稍后到达的 B 不会被 dedup 丢弃。
- 多 Agent、多文件、并行编辑按 batch 正确路由。
- 多 workspace root 生成独立 transports。
- shutdown/restart 和模块多实例互不干扰。

### LSP 真实集成

- broken -> fixed -> broken 三阶段。
- 快速连续三次保存，只呈现最终版本诊断。
- 同时编辑两个文件。
- Python、TS/JS、Rust 至少各覆盖一条多次编辑路径。
- remote backend 明确验证“禁用 Host LSP”或“路由到 Peer LSP”。

## 9. 与后续 CLI/TUI 重构的关系

subagent、LSP 和 ToolExecutor 应只产生结构化领域事件，不应直接决定 Rich/Textual 文案。后续 CLI/TUI 共用层至少需要统一：

- `ToolStarted/ToolFinished`
- `SubagentJobUpdated`
- `DiagnosticsPublished/DiagnosticsCleared`
- `ApprovalRequested/Resolved`
- `SessionChanged`
- `Model/Mode/ContextStateChanged`

UI 层再分别把这些事件映射为 Rich CLI block 或 Textual widget。尤其不要让 LSP Hook 直接拼接最终 UI 文案并同时承担模型上下文注入；“给模型看的诊断”和“给人看的诊断”应是同一结构化 batch 的两个 presenter。
