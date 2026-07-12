# ReuleauxCoder TUI 开工前 Definition of Done

> 日期：2026-07-11
> 状态：v0.4.0 全部完成；TUI 可以只作为新 adapter 开工。
> 目标：在开始建设 Textual TUI 前，完成共享运行事件、展示模型、命令效果、交互端口、生命周期和远端 CLI 收敛，避免让 TUI 承担现有架构债。

## 0. 验收结论（2026-07-12）

本清单的共享运行协议、presentation、command/view/interaction、Subagent/LSP、Extension scope、Remote Peer、WorkspacePort、配置与工程门禁均已落地。原始章节保留目标定义；第 17 节为最终勾选结果。复验入口：

```bash
uv run ruff check .
uv run pytest -q
go test ./...                         # 在 reuleauxcoder-agent/
uv build
RCODER_RUN_LSP_INTEGRATION=1 uv run pytest -q tests/extensions/lsp/test_integration_smoke.py
```

最终复验结果：Ruff 全仓通过；Python `707 passed, 24 skipped`；真实 LSP `15 passed, 1 skipped`；Go 全包通过；成功生成 `reuleauxcoder-0.4.0.tar.gz` 与 `reuleauxcoder-0.4.0-py3-none-any.whl`。真实 LSP 已执行 Python、TS 7 native、TS 6 legacy、JavaScript、YAML、Bash、Go、C、C++、Rust，以及 stale/多文件/多 root/父子隔离矩阵；单个 skip 是当前为空的 startup-only 参数集，不代表 server 缺失。

2026-07-12 的 FORGE 收口继续将 CLI adapter 拆为 theme、history、streaming、startup、interaction 和 typed command views；`CLIRenderer` 仅保留事件路由与兼容入口。Tool 动作文案已进入 Rich 无关的 presentation semantics，因此 TUI 不需要复制 CLI 对 tool name/argument 的解释。更改全局 CLI 风格只需替换 `CLITheme` 与对应 presenter 布局，不再跨 runtime、tool 或 command use case 修改。

## 1. 基本原则

TUI 不应成为推动底层抽象成形的试验场。开始 TUI 前，应先让现有 CLI 完整运行在共享 presentation 内核上，并让 remote CLI 复用同一展示路径。

目标关系：

```text
Runtime / Domain
  -> typed RuntimeEvent
  -> PresentationReducer
  -> TranscriptModel + RuntimeViewState
       -> CLI Rich Adapter
       -> Remote CLI Terminal Sink
       -> TUI Textual Adapter
```

CLI、TUI、Remote CLI 只能替换 adapter/sink，不应分别解释 Agent、Tool、subagent、LSP、approval 和 command 语义。

## 2. 必须完成的共享运行事件

替换或兼容迁移当前弱类型的 `UIEvent.message + data` 和缺少关联 ID 的 Tool 事件。

统一 event envelope 至少包含：

```text
event_id
timestamp
agent_id
session_generation
turn_id
correlation_id
```

建议的事件类型：

```text
TurnStarted / TurnFinished
AssistantContentDelta
ReasoningDelta
ToolStarted
ToolOutputDelta
ToolFinished
SubagentJobChanged
DiagnosticsPublished / DiagnosticsCleared
ApprovalRequested / ApprovalResolved
NotificationRaised
SessionChanged
RuntimeStateChanged
ViewRequested / ViewRefreshed
```

验收条件：

- Tool start/end 通过 `tool_call_id` 关联。
- Subagent 通过 `job_id` 关联。
- LSP 通过 diagnostic batch/document version 关联。
- Approval 通过 request ID 关联。
- 迟到事件能通过 session generation 被拒绝。
- CLI renderer 不再直接消费任意 `data` dict。

## 3. 必须完成的结构化 ToolOutcome

Tool 不再用一个字符串同时表达状态、结果、diff、错误、诊断和截断。

建议模型：

```text
ToolOutcome
  status
  summary
  content
  stdout
  stderr
  diff
  diagnostics
  exit_code
  duration
  truncation
  archive_reference
  metadata
```

验收条件：

- 成功/失败不再根据 `Error:` 字符串猜测。
- edit/write diff 使用结构化字段。
- LSP diagnostics 不再拼进 diff 字符串。
- 模型可见文本与 UI preview 分别生成。
- Hook 输出硬限制、事件传输限制和 UI preview 限制彼此独立。
- Local/Remote backend 对同一 Tool 生成语义一致的 ToolOutcome。

## 4. 必须完成的 Presentation 内核

实现：

```text
PresentationReducer
TranscriptModel
RuntimeViewState
PresentationPolicy
```

共享 transcript cell 至少包括：

```text
AssistantCell
ToolCell
DiffCell
DiagnosticCell
SubagentCell
NoticeCell
ApprovalCell
```

Reducer 负责：

- start/end correlation；
- streaming delta 合并；
- orphan event 处理；
- Tool/subagent 原地状态更新；
- notification 分级；
- transcript retention；
- compact/standard/debug 展示策略输入。

验收条件：

- 给定同一事件序列，Reducer 产生确定性、深度相等的 state。
- CLI/TUI 不自行推断 Tool 类型或输出类型。
- TranscriptModel 有明确 retention/virtualization 边界。
- 并行 Tool 和 subagent 不会更新错误 Cell。

## 5. 必须先迁移现有 CLI

现有 CLI 必须先成为新 presentation 的第一个正式 adapter。

CLI adapter 只保留：

- Rich theme/style mapping；
- Markdown streaming sink；
- prompt_toolkit 输入；
- append-only cell rendering；
- terminal width、ANSI、颜色处理。

需要移出 CLIRenderer 的内容：

- Tool output 压缩策略；
- diff 字符串探测；
- subagent 状态判断；
- notification 业务分类；
- Tool start/end 关联；
- archive/truncation marker 解析。

默认 compact 样式建议：

- assistant Markdown 不加外框；
- 普通 info/success 使用单行；
- warning/error/approval 才使用强调块；
- Tool start/end 合并为一个 lifecycle cell；
- read/glob/grep 成功默认显示摘要；
- shell 显示命令、exit code、duration 和 head+tail；
- edit/write 显示路径与修改统计，diff 可展开；
- subagent 不为 queued/running/completed 各打印独立 Panel；
- debug 模式才显示内部 kind/source/archive 信息。

验收条件：

- CLI 不再维护无用途的无界 `_completed_blocks`。
- 后台线程不直接写 Rich Console。
- 所有输出经过单一 CLI scheduler/output coordinator。
- 80/120 列 snapshots 通过。
- 现有 CLI 功能迁移完成后才创建真实 TUI App。

## 6. 必须完成的 CommandEffect

Command handler 不再同时直接发 UI bus 并返回 `view_requests`。

统一返回：

```text
CommandEffect
  control: continue | chat | exit
  notifications
  views
  interactions
  state_changes
```

Dispatcher 是唯一 effect 发布者。

验收条件：

- 删除 bus/open_view 与 return/view_requests 双通道。
- Command handler 可在没有 UI bus 的测试中纯运行。
- `extensions/command` 不再 import Rich、Textual 或 `interfaces.cli`。
- Command 异常转为 typed failure effect。

## 7. 必须完成的 Typed ViewModel

以下视图应建立 typed model：

- Help
- Models
- Modes
- Sessions
- Approval rules/effective policies
- MCP servers
- Skills
- Token/context usage
- Subagent jobs
- Effective config

例如：

```text
ModelListViewModel
  active_main
  active_sub
  rows
  diagnostics
  available_actions
```

验收条件：

- 业务 handler 不返回预渲染 Markdown 作为唯一数据源。
- 不使用任意 payload dict 作为正式跨 UI 契约。
- CLI presenter 将 ViewModel 转为 Rich。
- TUI presenter 以后可将相同 ViewModel 转为 Textual widget。

## 8. 必须完成的 InteractionCoordinator

业务层只产生 typed interaction request：

```text
ConfirmRequest
ChooseOneRequest
TextInputRequest
ReviewRequest
```

Coordinator 负责 request ID、等待、取消、超时和 response routing。

不同 adapter：

- CLI：同步阻塞输入；
- TUI：主线程 Modal/Screen，异步 resolve；
- Remote CLI：opaque request/reply 转发。

验收条件：

- Approval preview 由共享 service 构造一次。
- CLI/TUI/Remote 不各自拼 approval diff/Markdown。
- 后台 subagent approval 不与主输入抢 stdin。
- 取消和 session shutdown 会解决所有 pending interactions。

## 9. 必须修复的 Subagent 边界

- Job 在 Future submit 前原子登记。
- 子线程异常成为 failed，不包装为空 completed。
- Job 固化 parent agent/session generation。
- reset/new/restore 后旧 callback 不得注入。
- timeout 后取消能传播到 AgentLoop、LLM 和 Tool。
- Shell/process 有可执行的终止机制。
- 父 Agent 与 subagent 不共享有状态 Tool 实例。
- Hook/extension 按 Agent scope 重建，不浅拷贝 runtime service。
- App cleanup 关闭 SubagentManager pool，并处理 queued/running jobs。
- Job 有 retention/pruning。

## 10. 必须修复的 LSP 边界

- Worker 每轮只从实际执行的队列 pop，不能丢 tool/diagnostics/notification。
- 文档 version 单调递增。
- `publishDiagnostics` 按 URI 覆盖，空结果清理旧错误。
- 等待诊断基于 baseline generation，只接受更新通知。
- `seq`/document version 真正参与 stale rejection。
- DiagnosticBatch 替代全局 drain 和 `_diagnostics_fed` bool。
- Batch 按 agent/session/turn/tool call/file 路由。
- Subagent 默认不继承父 Agent 的 diagnostics consumer。
- 多 workspace root 使用独立 transport。
- Remote backend 不在 Host 的错误文件视图上运行 LSP。
- shutdown/restart 清理 Hook 和 Tool 中的 manager 引用。

真实集成测试必须覆盖：

- broken -> fixed -> broken；
- 连续快速保存三次；
- 同时编辑两个文件；
- parent/subagent 并行；
- 多 workspace root；
- remote workspace 策略。

## 11. 必须完成的内部 Extension/Hook 收敛

TUI 前不必公开完整第三方 plugin API，但内部机制必须稳定。

需要完成：

- 固定 Core Tool Pipeline。
- 拆出 AuthorizationPolicy、ContextContributor、OutcomeProcessor、RuntimeObserver、LifecycleParticipant。
- Observer 使用 immutable snapshot，不允许修改结果。
- 建立 ExtensionManifest 与内部 ExtensionManager。
- Contribution 声明 scope、subagent policy、remote compatibility、thread safety。
- 使用 phase + before/after constraints，替代纯数字 priority。
- Scope container 负责 materialize 和逆序 disposal。
- 删除 `hook_registry._hooks` 私有遍历。
- 删除模块全局 LSP manager。
- 删除 `setattr(agent, ...)` 式依赖注入。
- Hook failure 产生结构化 diagnostic，不静默吞掉。
- Guard warning 有明确 UI/audit 消费路径。
- `/new`、restore、save、shutdown 统一经过 lifecycle coordinator。

公开 Python entry-point plugin API 应等待内部 builtin manifest 经历至少一轮稳定迁移后再进行。

## 12. 必须完成的 Remote CLI 收敛

Host 必须拥有：

- Agent、LLM、config、mode、session；
- slash command；
- approval policy；
- Hooks/extensions；
- Tool schema、validation、ToolOutcome；
- PresentationReducer 和 CLI 样式。

Peer 只保留：

- stdin/stdout CLI；
- terminal capabilities；
- auth/heartbeat/long-poll/reconnect；
- opaque 用户输入；
- generic interaction response；
- 最小远端原语；
- cancellation/deadline/process cleanup。

必须完成：

- Remote handler 使用同一 PresentationReducer/CLIRenderer。
- 每个 Peer session 有独立 presentation state。
- 删除临时 command bus history replay。
- 删除 remote handler 手写 Rich startup/approval 展示。
- Peer 删除 glamour/Markdown。
- Peer 不解释产品 slash command，除本地 transport control。
- Approval 改 generic interaction。
- `peer_token_ttl_sec` 真正接入。
- Token 可续签。
- Poll 使用 server-side long poll。
- 网络错误 exponential backoff + jitter。
- 401 触发 refresh/re-register。
- Tool request 支持 cancel/deadline/idempotency。
- Protocol 有 version/capability negotiation。
- Capabilities 真正参与 dispatch。

## 13. WorkspacePort 迁移要求

需要访问目标工作区或进程的产品 Tool 面向统一 Port：

```text
LocalWorkspacePort
RemoteWorkspacePort(peer_id)
```

Peer 最终只实现：

```text
process.start / input / cancel
fs.stat
fs.read
fs.write_atomic
fs.replace_exact_atomic
fs.list
fs.search（可选加速能力）
```

Tool schema、参数校验、审批、diff、错误文案、截断和展示只在 Host 实现。

迁移顺序：

1. read/write/edit；
2. list/glob；
3. grep/search；
4. shell/process streaming。

现有 `exec_tool` 可作为 protocol v1 兼容；每迁移一个 Tool，删除对应 Go 产品语义，并用共享 fixtures 验证 Local/Remote ToolOutcome 一致。

## 14. 必须清理的配置

- 修复 `lsp.include_warnings` 默认值不一致。
- 明确 LSP 是否默认开启，并显示 effective state。
- 将 `active` legacy alias 限制在 migration 层。
- 无效 model profile 不再静默回退。
- 让 `session.auto_save` 真正生效，或删除配置项。
- 分开 model output cap、archive policy、UI preview cap。
- 分开 provider reasoning 参数与 UI reasoning display。
- 提供 effective config view，显示 default/global/workspace/session/CLI 来源。
- 对无消费者、未生效和 legacy 配置产生 diagnostic。
- UI 初期只暴露 compact/standard/debug 等语义配置，不固化颜色和边框实现细节。

## 15. 工程护栏

### Architecture fitness tests

- `extensions/command` 不得导入 `interfaces.cli`、Rich、Textual。
- presentation 层不得导入 Rich/Textual、文件系统、LLM client。
- domain/runtime event 不得包含 Rich markup 或 Textual CSS class。
- Core approval pipeline 不得被 plugin 绕过。

### Presentation tests

- RuntimeEvent 深度相等和序列化测试。
- Reducer deterministic state 测试。
- 并行 correlation/orphan event 测试。
- CLI 80/120 列 snapshots。
- compact/standard/debug snapshots。
- 同一事件序列在不同 adapter 产生语义等价 transcript。

### Cross-language tests

- Python encoder -> Go decoder。
- Go encoder -> Python decoder。
- Protocol schema drift check。
- N/N-1 compatibility。
- Local/Remote ToolOutcome deep equality。
- Local CLI/Remote CLI terminal snapshots。

### CI

- Go unit tests。
- Linux/Windows/macOS peer build。
- amd64/arm64 artifact smoke。
- Bootstrap checksum/signature。
- Peer binary dependency和体积预算。

## 16. 实施顺序

```text
A. Characterization snapshots + architecture boundary tests
B. RuntimeEvent + ToolOutcome + correlation IDs
C. PresentationReducer + TranscriptModel + PresentationPolicy
D. CLI 完整迁移到共享 presentation
E. CommandEffect + Typed ViewModel + InteractionCoordinator
F. Subagent/LSP 生命周期、stale 和路由修复
G. Core Pipeline + Extension scope/container
H. Remote CLI 接共享 presenter并瘦 Peer
I. WorkspacePort 逐步迁移
J. 配置收敛和 effective config diagnostics
K. 开始真实 TUI
```

不要 Big Bang。每个阶段必须保留 legacy adapter，并在 snapshots/contract tests 通过后删除旧路径。

## 17. TUI 开工门槛

只有以下条件全部满足，才开始真实 Textual TUI：

- [x] CLI renderer 不再直接消费裸 AgentEvent/UIEvent dict。
- [x] ToolOutcome 已结构化。
- [x] Tool/subagent/diagnostics/approval 有 correlation ID。
- [x] PresentationReducer 已成为 CLI 的正式路径。
- [x] Command 只有一条 effect 输出通道。
- [x] Builtin command 不再依赖 CLI/Rich。
- [x] 主要 command views 使用 Typed ViewModel。
- [x] Approval 使用共享 InteractionCoordinator/ViewModel。
- [x] Subagent reset/timeout/cleanup 边界已修。
- [x] LSP stale、队列丢失和 batch 路由已修。
- [x] Extension scope 与 disposal 已落地。
- [x] Remote CLI 使用同一 presenter。
- [x] Peer 不再处理 Markdown、approval 或产品展示。
- [x] 关键配置默认值和 effective config 已收敛。
- [x] CLI snapshots、Reducer tests、protocol fixtures 和 Go tests 已进入 CI。

完成这些条件后，TUI 的职责只剩 Textual adapter、布局、focus、keyboard、resize、virtualized transcript 和 modal，不需要再重构业务语义。
