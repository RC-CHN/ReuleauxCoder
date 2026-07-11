# ReuleauxCoder Extensions / Hooks / Remote Peer 收敛方案

> 日期：2026-07-11
> 状态：v0.4.0 内部收敛与薄 Peer 改造已实施并验收；正文保留开工前问题基线。
> 目标：收敛内部扩展机制，避免过早形成不可维护的插件 API；把远端 Peer 降为薄 CLI transport/executor，使 Host、本地 CLI、远端 CLI 复用同一运行和展示路径。

## 0. v0.4.0 实施闭环（2026-07-12）

- Core Tool Pipeline 已固定授权、执行、outcome processor、context contributor、observer 与 runtime event 的顺序；observer 使用不可变 snapshot，失败产生结构化诊断。
- `ExtensionManifest`/`ExtensionManager` 已覆盖 API version、依赖排序/cycle、scope materialize、subagent omit/rebind、lifecycle exactly-once 和逆序 disposal。
- Tool 与 Hook 必须显式 `clone_for_scope`；subagent 获得独立的有状态 Tool，LSP consumer 不再因浅拷贝跨 Agent 泄漏。
- Remote Host 复用 Local CLI presenter；Go Peer 已移除 glamour、Markdown、产品 Tool、产品 slash command及 approval 专用语义，只处理 terminal frame、generic interaction、连接和 OS 原语。
- read/write/edit/list/glob/grep/shell 均由 Host Tool 复用产品语义，并通过 Local/Remote WorkspacePort 或 ProcessPort 原语执行；Peer 保留 root confinement、atomic fs 与 process start/input/poll/cancel。
- token TTL/续签、long poll、backoff+jitter、401 refresh、cancel/deadline/idempotency、protocol/capability negotiation 已接入并测试。
- Python/Go 共用 N/N-1 contract fixtures；CI 构建 Linux/macOS/Windows 的 amd64/arm64 Peer，执行 Go tests、体积/依赖门禁并发布 SHA256SUMS，bootstrap 校验 checksum 与大小上限。

## 1. 总体结论

当前项目有“扩展点”，但还没有真正的插件系统：

- Hooks 使用全局 decorator registry；
- Tools 使用另一套全局 class registry；
- Commands 使用 module registrar；
- Views 使用 UI-target decorator registry；
- Skills 扫描文件系统；
- MCP 自己维护 server/tool 生命周期；
- LSP 和 Remote Relay 由 AppRunner 特殊装配。

这些机制各自可用，但缺少统一的声明、实例化、作用域和 disposal。此时直接公开“第三方插件 API”，会把当前的全局状态、`setattr()`、浅拷贝和 UI 反向依赖固化下来。

推荐顺序：

1. 先把核心 pipeline 和内部 extension composition 收稳。
2. 再定义最小、版本化、可测试的 plugin contribution API。
3. 同时把 remote peer 缩到 stdlib-only 的薄 CLI adapter + 最小远端原语。
4. Host 继续拥有 Agent、commands、approval、Hooks、session、presentation 和最终终端样式。

## 2. 术语应先分清

建议固定以下概念：

| 概念 | 定义 |
|---|---|
| Core Pipeline | 运行正确性和安全性必需的固定步骤，不允许插件任意重排 |
| Hook | 在稳定边界观察或有限扩展运行行为，不代表可分发包 |
| Extension | 进程内组成单元，可由 builtin 或 plugin 提供 |
| Plugin | 带 manifest/API version 的可安装 extension package |
| Skill | 提示词/工作流知识包，不执行任意 Python 代码 |
| MCP Server | 进程外工具/资源提供者，拥有独立协议和生命周期 |
| UI Adapter | CLI/TUI/remote CLI 的展示与交互实现，不属于业务 plugin |

不要把 Skill、MCP、Hook、Plugin 合并成一个万能概念。它们的安全边界和生命周期不同。

## 3. 当前 Hook 机制的问题

### 3.1 Discovery 依赖 import side effect

`discover_hook_specs()` 手工 import builtin classes，decorator 将 spec 写入模块全局 `_HOOK_SPECS`。这不是外部 discovery，也缺少：

- plugin manifest；
- API version；
- duplicate ID 检查；
- config namespace；
- dependency/order constraints；
- unload/dispose；
- runtime enable/disable；
- diagnostics。

`enabled_by_default` 虽存在，但没有完整用户配置和运行期管理路径；`HookSpec.factory` 也没有由 decorator 正常传入的公开用法。

### 3.2 数字 priority 不足以表达顺序

Hook 只按 priority 降序执行。多个 extension 使用相同 priority 时依赖注册顺序；无法声明：

```text
after = authorization
before = output_archiver
requires = lsp_manager
conflicts = other_hook
```

插件增多后，数字 priority 会成为隐式协议。

### 3.3 Scope 与 clone 语义缺失

Subagent 通过 `copy.copy(hook)` 浅拷贝所有 Hook，导致 LSP manager、UI bus 或其他内部 service 被共享。当前 Hook 无法声明：

- app singleton；
- session-scoped；
- agent-scoped；
- turn-scoped；
- subagent omit/share/rebind/new-instance；
- remote-agent compatible。

### 3.4 核心 pipeline 与插件 Hook 混在一起

当前 builtin Hook 实际承担不同性质的职责：

- `ToolPolicyGuardHook`：核心授权策略；
- `ToolOutputTruncationHook`：ToolOutcome 持久化/规范化；
- `ProjectContextHook`：模型上下文 contributor；
- LSP hooks：诊断 producer/consumer/router；
- startup notifier：生命周期 observer。

这些不应都被抽象成同一种通用 Hook。审批、工具配对、输出归档等核心不变量必须是固定 pipeline stage。

### 3.5 Observer 契约被破坏

`ObserverHook` 文档声明不改变 control flow，但 LSP edit observer 会修改 `context.result`。目前只是因为 context 是可变对象而“碰巧工作”。

建议 observer 接收 immutable snapshot，只能发事件；需要修改结果的逻辑必须是显式 processor/transform。

### 3.6 失败语义太粗且不可观测

- guard exception：fail closed；
- transform exception：直接传播；
- observer exception：完全吞掉。

类别默认值可以保留，但每个 spec 需要显式 failure policy、timeout 和 diagnostic。Observer 静默失败会使 LSP 等功能表现为偶发失效。

### 3.7 Guard warning 没有完整消费路径

Policy 可返回 `GuardDecision.warn()`，但 ToolExecutor 主要处理 deny 和 requires_approval，没有把普通 warning 可靠发布给 UI/审计记录。配置中的 `warn` 因而可能缺少用户可见效果。

### 3.8 生命周期触发不一致

- startup/session-start 主要由 AppRunner 初次 initialize 触发；
- `/new`、reset、restore 没有统一通过 session lifecycle coordinator；
- session-save 在 command 模块中手工触发；
- CLI cleanup 调用通常没有把 agent 传给 `runner.cleanup(agent)`，runner-shutdown Hook 可能根本不执行；
- remote peer Agent 每次 chat 重建并重新注册 Hooks，但没有对称 disposal。

## 4. 建议的 Core Pipeline

工具主链应固定为：

```text
resolve tool
  -> schema/preflight validation
  -> mode/capability check
  -> authorization policies
  -> approval coordination
  -> execution target dispatch
  -> normalize ToolOutcome
  -> outcome processors (archive/truncate/diagnostics metadata)
  -> persist model-visible result
  -> publish typed runtime events
```

以下属于 core，不应允许 plugin 删除或绕过：

- tool call/result 配对；
- mode/capability enforcement；
- authorization/approval；
- cancellation/deadline；
- structured ToolOutcome；
- output size hard cap；
- session/turn/call correlation；
- audit/runtime event publication。

将现有 Hook 拆成更窄的 extension ports：

```text
AuthorizationPolicy
  evaluate(ToolRequest) -> PolicyDecision

ContextContributor
  contribute(ContextRequest) -> list[ContextFragment]

OutcomeProcessor
  process(ToolOutcome) -> ToolOutcome

RuntimeObserver
  observe(RuntimeEvent) -> None

LifecycleParticipant
  start(scope) -> Disposable
```

如果仍保留通用 TransformHook，只应开放少数稳定、明确版本的上下文，并限制可修改字段。

## 5. Extension / Plugin 目标模型

### 5.1 Manifest

```text
ExtensionManifest
  id
  version
  api_version
  display_name
  description
  trust_level
  config_namespace
  requires
  contributions
```

Contribution 可以包含：

```text
tool_factories
command_specs
authorization_policies
context_contributors
outcome_processors
runtime_observers
lifecycle_participants
```

第一版不要允许 plugin 直接贡献 Rich/Textual renderer；plugin 只能贡献 typed ViewModel 或 semantic action，UI adapter 决定如何显示。

### 5.2 ExtensionContext

Extension factory 应接收一个窄、类型化的 context，而不是任意 Agent/AppRunner：

```text
ExtensionContext
  config_view
  event_publisher
  workspace
  session_identity
  service_resolver (受限协议)
  cancellation
```

禁止继续依赖：

- `setattr(agent, ...)`；
- 遍历 `hook_registry._hooks`；
- 模块全局 `set_lsp_manager()`；
- extension 直接 import CLI renderer；
- shallow copy runtime service。

### 5.3 Scope policy

每个 contribution 显式声明：

```text
scope: app | session | agent | turn
subagent: omit | share | rebind | new_instance
remote: host_only | peer_aware | target_agnostic
thread_safe: true | false
```

Subagent 不再 clone 整个 registry，而是让 ExtensionManager 为新的 AgentScope 重新 materialize contributions。

### 5.4 Ordering

使用 phase + before/after constraints：

```text
phase = authorize | context | outcome | observe
before = [extension-id:component]
after = [core:normalize-outcome]
```

启动时拓扑排序；重复 ID、缺失依赖或循环依赖直接产生 config diagnostic。

### 5.5 生命周期与 disposal

每个启动的 extension 返回 `Disposable/AsyncDisposable`。App、session、Agent、remote peer session 结束时由 scope container 逆序释放。

这也应成为 SubagentManager、LspManager、MCPManager、remote session 的统一清理入口。

### 5.6 Plugin discovery

内部 builtin 先使用显式 manifest 列表；稳定后再支持 Python entry points，例如：

```text
group = reuleauxcoder.plugins
```

第三方 Python plugin 是进程内完全信任代码，必须明确提示。低信任外部工具优先使用 MCP，不应假装 Python plugin 有隔离能力。

## 6. Remote Peer 当前维护债

### 6.1 Peer 同时承担了四种职责

Go peer 当前同时负责：

1. 注册、heartbeat、poll、chat stream transport；
2. shell/read/write/edit/glob/grep/list_file 实现；
3. Markdown/ANSI/plain 输出选择；
4. approval 文案展示和 allow/deny 语义。

这导致 Host 与 Peer 各有一套 CLI/presentation/interaction 逻辑。

### 6.2 Go 端重写了 Python Tool 语义

`internal/tools/execute.go` 超过 700 行，无任何 Go 测试。已经存在或容易出现的漂移：

- capabilities 注册未包含 `list_file`，executor 却支持；Host 也未严格校验 capability；
- Python/Go globstar、grep include、skip dirs、排序和输出格式需人工同步；
- Python write 返回完整 diff，Go write 只返回行数；
- Go edit 只根据 old/new 片段构造伪 unified diff，不包含真实上下文；
- shell 选择、stderr 合并、timeout、truncate 与 Python 不同；
- read 的编码、超长行 Scanner 限制和 offset 行为不同；
- `workspace_root` 只上报，不约束绝对路径或 `..`；
- `resolvePath()` 接受任意绝对路径，workspace confinement 并未实现。

### 6.3 Host remote handler 重做了一套 CLI

Host 的 `_stream_chat` 会：

- 创建 peer Agent；
- 特判 slash command；
- 建临时 command bus 并重放 history；
- 创建 Rich recording Console；
- 手工构造 startup Panel；
- 重做 approval diff/Markdown payload；
- 手工翻译 AgentEvent 为 remote chat event；
- 再把 Rich ANSI 发给 Peer。

这条路径没有经过统一 presentation reducer，和本地 CLI/TUI 会持续漂移。

### 6.4 Token 配置没有生效，也没有续签

配置存在 `peer_token_ttl_sec`，但 RelayServer 不接收该参数，注册时硬编码 `ttl_sec=3600`。Peer token 一小时后失效，heartbeat/poll 返回 unauthorized；Go poll loop 遇到错误后直接退出，没有 refresh/re-register。

### 6.5 连接恢复体验不足

- poll/chat HTTP error 直接终止；
- 没有 exponential backoff/jitter；
- 没有 token refresh；
- 没有稳定 device/peer identity 恢复；
- 没有 resume cursor 持久化；
- tool request 没有显式 cancel envelope；
- Ctrl+C 主要终止整个 peer，不能优雅区分 cancel current turn 与 disconnect。

### 6.6 Poll 低效且 timeout 边界紧

tool poll 没有 server-side wait，无工作时立即返回 noop，默认每 500ms 请求一次。Chat stream 使用约 30 秒 long poll，但 Go HTTP client 全局 timeout 也是 30 秒，存在临界竞态。

### 6.7 Go 依赖主要为了 Peer 侧 Markdown

`glamour` 及大量 indirect dependencies 主要服务 Markdown 渲染。如果 Host 统一输出 terminal frame，Peer 可移除 glamour，恢复接近 stdlib-only 的小二进制。

## 7. 薄 Peer 目标边界

### 7.1 Host 必须拥有

- Agent/LLM；
- model/mode/config；
- session/history；
- slash command parsing；
- approval policy 和 approval view model；
- Hooks/extensions；
- Tool schema、validation 和 ToolOutcome normalization；
- presentation reducer；
- CLI 样式、Markdown/diff rendering；
- subagent/LSP/MCP orchestration。

### 7.2 Peer 只保留

- CLI stdin/stdout；
- terminal capability 上报：width、color、unicode、TTY；
- auth、heartbeat、long-poll/reconnect；
- 打印 Host 生成的 terminal frame；
- 把用户输入作为 opaque input event 发给 Host；
- 把 generic interaction response 发给 Host；
- 最小远端执行原语；
- cancellation、deadline、process cleanup；
- OS/arch/workspace metadata。

Peer 不应理解：

- Markdown；
- Rich/Textual；
- slash command 语义（除本地 `exit/disconnect` 控制）；
- approval allow/deny policy；
- model/mode/session；
- subagent；
- diagnostics；
- Tool 输出如何展示。

### 7.3 远端 CLI 与本地 CLI 共用同一 presenter

```text
Agent RuntimeEvent
  -> PresentationReducer
  -> DisplayCell / InteractionRequest
  -> CLIRenderer
       -> LocalTerminalSink
       -> RemoteTerminalSink(peer capabilities)
```

RemoteTerminalSink 将 Host 生成的 ANSI/plain frame 写入 chat event；Peer 只打印 bytes。这样本地 CLI 与远端 CLI 的 compact policy、Tool 样式、diff、notification 和 command view 自动一致。

每个 Peer session 拥有独立 renderer state，不能每次 chat 临时重建、重放 bus history。

### 7.4 Generic interaction

协议改为通用交互，而不是 approval 专用：

```text
InteractionRequested
  request_id
  kind: confirm | choose_one | text | secret | review
  rendered_frame
  input_constraints

InteractionReplied
  request_id
  value
  cancelled
```

Peer 只收集输入；Host 校验并决定是否重新提问。Approval 只是 Host 上的一种 interaction use case。

## 8. 如何减少远端 Tool 重复实现

长期目标不是让 Peer 实现 `read_file/edit_file/glob/...` 这些产品 Tool，而是提供稳定的 `WorkspaceTransport` 原语：

```text
process.start / process.input / process.cancel
fs.stat
fs.read
fs.write_atomic
fs.replace_exact_atomic
fs.list
fs.search (可选加速 capability)
```

Host 上的同一 Tool class 面向 `WorkspacePort`：

```text
LocalWorkspacePort
RemoteWorkspacePort(peer_id)
```

Tool schema、参数校验、approval、结果结构、diff、truncate、presentation 都只在 Host 实现一次。

需要留在 Peer 的原子操作：

- `replace_exact_atomic`：避免 Host read 后 write 的竞争窗口；
- `write_atomic`：保证临时文件 + rename 等平台语义；
- process execution/cancel；
- 可选 server-side search：避免把整个 workspace 拉回 Host。

即便保留 `fs.search`，它也返回结构化 raw matches，不生成用户文案。

### 迁移期

现有 `exec_tool` 暂保留为 protocol v1 compatibility。按工具逐步迁移：

1. read/write/edit；
2. list/glob；
3. grep/search；
4. shell/process streaming。

每迁移一个 Tool，Host local/remote 必须使用相同 ToolOutcome fixtures。v2 完成后删除 Go 产品 Tool 实现。

## 9. Remote 协议建议

### 9.1 Handshake

```text
protocol_version
peer_build_version
device_id
os/arch
terminal_capabilities
workspace_root
primitive_capabilities
```

Host 明确协商 protocol version；不支持时给出可读升级提示。Capabilities 必须实际参与 dispatch 校验。

### 9.2 Transport

为控制维护债，可以继续使用 Go/Python 标准库 HTTP，不必立即引入 WebSocket：

- `/poll` 改成 20～25 秒 server-side long poll；
- heartbeat 可 piggyback 在 poll；
- HTTP client timeout 必须大于 long-poll deadline；
- transient error exponential backoff + jitter；
- 401 触发 token refresh/re-register 流程；
- Host 返回 refreshed peer token/expiry；
- request 使用 request_id、deadline、cancel_id；
- result submission 支持幂等去重。

只有确实需要高频双向输入流时再切 WebSocket，不要为了“现代”增加第二套 transport。

### 9.3 Protocol schema

当前 Python dataclass 和 Go struct 手工维护，且大量 `map[string]any/dict`。建议提供 canonical JSON Schema/IDL，并生成或至少校验双方类型。

共享 contract fixtures 覆盖：

- register/renew/disconnect；
- input/display frames；
- interactions；
- process stream/result/cancel；
- fs operation/result；
- error codes；
- version negotiation。

## 10. Peer CLI 体验建议

默认输出保持克制：

```text
Connected to https://host
Workspace: /path/project
Session: abc123

You ›
```

- 不打印大边框 READY banner；
- connection/reconnect 使用单行状态；
- Host renderer 决定 Agent、Tool、diff 样式；
- 首次 Ctrl+C 发送 cancel current turn；空闲时 Ctrl+C 请求退出；再次 Ctrl+C 强制退出；
- 断线时保留当前输入，后台重连并显示退避时间；
- token 即将过期时无感续签；
- Host 版本不兼容时明确给出 peer upgrade 命令；
- 本地只保留 `/exit`、`/disconnect`、`/connection` 等 transport control，不自行实现产品命令。

## 11. 测试与维护门槛

### Hook / Extension

- manifest duplicate/API version/config validation；
- ordering DAG/cycle；
- scope materialization；
- subagent omit/rebind；
- lifecycle reverse disposal；
- observer failure diagnostic；
- warning policy可见；
- plugin 不可绕过 core approval pipeline。

### Peer Go 单元测试

- register/refresh/reconnect/backoff；
- long poll timeout；
- process streaming/cancel/exit code；
- atomic fs operations；
- workspace confinement policy；
- protocol fixtures；
- interaction input forwarding；
- ANSI frame passthrough；
- Ctrl+C 状态机。

### 跨语言 contract

- Python encoder -> Go decoder；
- Go encoder -> Python decoder；
- N/N-1 protocol compatibility；
- local/remote ToolOutcome deep equality；
- 同一 RuntimeEvent 序列在 local CLI 和 remote CLI 生成一致 terminal snapshot（允许终端宽度差异）。

### CI

- Go tests；
- linux/windows/darwin build；
- amd64/arm64 artifact smoke；
- bootstrap checksum/signature；
- protocol schema drift check；
- peer binary size/dependency budget。

## 12. 推荐实施顺序

### Phase A：先统一 presentation 和 command effect

沿 CLI/TUI 架构说明完成 typed RuntimeEvent、PresentationReducer、CommandEffect 和 TerminalSink。Remote handler 暂时接入同一 CLIRenderer，删除临时 command bus replay 和手写 Rich rendering。

### Phase B：Peer UI 瘦身

- Host 发送 terminal frames；
- Peer 删除 glamour/Markdown；
- approval 改 generic interaction；
- Peer 不再解释 tool/chat display event。

完成后 Go peer 可接近 stdlib-only。

### Phase C：连接可靠性

- 修 `peer_token_ttl_sec` wiring；
- token renewal；
- long poll；
- reconnect/backoff；
- cancel envelope；
- version/capability negotiation。

### Phase D：Extension/Hook scope container

- 固定 core pipeline；
- 拆 AuthorizationPolicy/ContextContributor/OutcomeProcessor/Observer；
- ExtensionManifest + ExtensionManager；
- subagent/remote Agent 按 scope 重新 materialize；
- 对称 disposal。

### Phase E：WorkspacePort

逐个把 Python Tool 从 backend-specific handler 改为 Local/Remote WorkspacePort，共享 ToolOutcome。Peer 产品 Tool 实现逐步删除。

### Phase F：公开 plugin API

内部 builtin manifest 和生命周期稳定、经过至少一轮版本迁移后，再开放 Python entry-point plugin。第一版 API 面要小，避免承诺任意 UI renderer 或任意 pipeline mutation。

## 13. 最终约束

未来新增能力时应满足：

1. 产品命令、approval、presentation 只在 Host 实现。
2. Peer 不增加新的用户展示逻辑。
3. Peer 不增加产品级 Tool 文案或 diff/truncate 规则。
4. 能通过 WorkspacePort/Host forwarding 完成的，不在 Peer 重写。
5. 必须留在 Peer 的逻辑应是最小、结构化、无 UI 语义的 OS 原语。
6. Core pipeline 的安全步骤不能被 plugin/hook 绕过。
7. Subagent、remote Agent、local Agent 使用同一个 extension scope materializer。
8. CLI、TUI、remote CLI 使用同一 presentation reducer，只替换 adapter/sink。
