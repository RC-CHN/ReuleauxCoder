# ReuleauxCoder CLI / TUI 开工前架构说明

> 日期：2026-07-11
> 状态：v0.4.0 前置改造已实施并验收；正文保留开工前问题基线。
> 目标：在优化 CLI 样式、降低冗长度和建设 Textual TUI 前，先固定可共享的事件、展示模型、交互端口和配置边界。

## 0. v0.4.0 实施闭环（2026-07-12）

- `domain/runtime` 与 `presentation` 已提供可序列化的 typed RuntimeEvent、确定性 reducer、bounded transcript、展示策略和结构化 Tool/Diff/Diagnostic cell。
- Local CLI 与 Remote CLI 均通过持久 presentation bus 消费同一 reducer；`CLIRenderer` 不再直接依赖 legacy `AgentEvent`，legacy bridge 只留在运行边界。
- Command 统一产出 `CommandEffect`；Help、Models/Modes、Sessions、Approval、MCP、Skills、Subagent、Effective Config 使用 typed ViewModel，业务 command 不依赖 Rich/CLI。
- approval/choice/text/review 统一进入 `InteractionCoordinator` 和共享 presenter，local/remote 的 review frame 有 80/120 列等价测试。
- compact/standard/debug 策略、shell duration/head-tail、结构化 diff stats、单行通知和 tool lifecycle 已完成 CLI 冗长度收敛。
- TUI 可以从 RuntimeEvent、Reducer、ViewModel、InteractionRequest 与 scheduler 边界直接复用，不需要复制 Agent、Tool、Command 或 approval 语义。

## 1. 当前判断

现有 CLI 已经具备 EventBus、UIRegistry、ViewRendererRegistry、UIInteractor 等抽象雏形，但抽象停在“接口名字”层面，真正的展示语义仍散落在：

- `AgentEvent` 字符串字段；
- `UIEvent.message + data: dict`；
- `CLIRenderer` 对 tool name、字符串 marker 和特殊 data key 的判断；
- command handler 直接发送 UI 文案；
- command extension 内部直接注册 CLI Rich renderer；
- approval handler/interactor 自行拼装展示 section；
- startup、remote relay 等位置直接构造 Rich `Panel`。

如果现在直接实现 TUI，TUI 会被迫复制 CLI 的推断逻辑，之后每种新事件都要同时修改 CLI/TUI。应先建立“运行事件 -> presentation reducer -> display model -> UI adapter”的稳定边界。

## 2. 已确认的耦合与样式问题

### 2.1 事件类型弱，Renderer 被迫猜语义

`UIEvent` 只有：

```text
message: str
level: UIEventLevel
kind: UIEventKind
data: dict[str, Any]
```

例如 reasoning 依赖 `data["is_reasoning"]`，remote stream 依赖 `data["remote_stream"]`，structured view 依赖 `data["view_type"]`。这些没有穷举约束、payload schema 或关联 ID。

Tool 事件也没有 `tool_call_id`、duration、output kind、truncation metadata、archive path；并行工具无法可靠把 start/end 合并到同一 display cell。

### 2.2 Tool result 在领域事件层先被截成 500 字符

`AgentEvent.tool_call_end()` 会将 result 截到 500 字符，CLI renderer 后续的 1200 字符、5/20 行压缩策略实际上拿不到完整输入。预截断 header/footer、archive path 和 diff 都可能在进入 UI 前丢失。

模型上下文 truncation、事件传输 cap、CLI 展示 cap 是三个不同责任，不应复用同一字符串截断。

### 2.3 Tool 展示依赖字符串探测

- 只有 `edit_file` 且 result 包含 `---` 才按 diff 渲染。
- `write_file` 同样返回 diff，却不会使用 diff renderer。
- edit result 后追加 LSP diagnostics 时，整个字符串仍可能被当作 diff。
- `[truncated]`、BEGIN/END marker 由 CLI 重新解析。
- Tool 返回 `Error: ...` 字符串时，事件仍可能标为 success。

需要结构化 `ToolOutcome`，至少区分：status、summary、stdout、stderr、diff、diagnostics、truncation、archive reference。

### 2.4 输出通道重复

部分 command handler 同时：

1. `ui_bus.open_view(...)`；
2. 返回 `CommandResult.view_requests=[...]`。

当前 CLI 主要依赖 bus，`view_requests` 基本未消费。TUI 接入后若同时消费两条通道，会重复打开视图。

同样，handler 大量直接 `ui_bus.info/success/error`，但 `CommandResult.notifications` 基本闲置。命令不是纯 use case，测试和复用都受限。

### 2.5 Command extension 直接依赖 CLI Rich renderer

`extensions/command/builtin/{model,mode,skills,system,approval}.py` 等直接导入 `interfaces.cli.views.common`；sessions、MCP command 甚至直接构造 Rich `Panel/Markdown`。

这使“builtin command”同时承担业务、payload 构造和 CLI adapter 职责，是 TUI 复用的直接阻碍。

### 2.6 默认样式过重

通用 notification 无论重要性均渲染为带边框 Panel，标题还是 `KIND · LEVEL`。Tool start、tool error、subagent completion、reasoning、structured command 又各自套 Panel。

典型一次子代理可能出现：

- manager 的 queued notification；
- running notification；
- completed notification；
- `SUBAGENT_COMPLETED` Panel；
- agent tool end output。

信息本身不一定重复，但视觉层级没有区分“瞬时状态、历史记录、需要操作的事项”。

### 2.7 CLI transcript 状态无界且没有实际用途

`CLIRenderer._completed_blocks` 会保存完整 session 的 content/tool/notification blocks，但 CLI 只向终端 append，不用它做重绘。长会话会重复占用内存。

TUI 确实需要 transcript model，但应由共享 reducer 管理，并提供 retention/virtualization；不能沿用 CLI renderer 私有列表。

### 2.8 后台线程与终端输入边界不清

UIEventBus 默认同步 dispatch。subagent callback 可在后台线程直接进入 Rich renderer，同时 REPL 使用 prompt_toolkit、Interactor 又调用原生 `input()`。可能出现 prompt 被打断、输出交错、approval 输入体验不一致。

TUI 需要主线程 message pump；CLI 也应通过单一 output coordinator 串行化渲染。

### 2.9 UIRegistration 能力不足

目前 registration 只有 profile、view registry、interactor，没有：

- renderer/event sink；
- UI scheduler/main-thread dispatch；
- application lifecycle；
- transcript/state store；
- command/input adapter；
- modal/overlay coordinator。

因此它还不能真正描述一个可启动的 TUI implementation。

## 3. 配置中的反直觉点

### 3.1 LSP 隐式默认开启

配置完全没有 `lsp` section 时，`LspConfig.from_config()` 返回 enabled=True。用户没有显式启用也会 health-check 多种 server，并可能在首次使用时通过 npx 拉起 package。

建议至少在生成配置和启动摘要中显示 effective state；是否改为 opt-in 需产品决策和兼容迁移。

### 3.2 `include_warnings` 默认值不一致

- 直接 `LspConfig()`：`include_warnings=True`；
- 存在任意 `lsp` section 但未写该字段：解析为 False；
- 不存在 `lsp` section：又回到 True。

即用户只写 `lsp.enabled: true`，warnings 行为就会反转。

### 3.3 模型激活字段过多且无效值静默回退

同时存在 `active`、`active_main`、`active_sub`，运行态又有 `active_model_profile`、`active_main_model_profile`、`active_sub_model_profile`。无效名称会静默回退到第一个 profile，用户难以知道实际用了哪个。

建议 persisted schema 只保留 main/sub 两个概念；legacy alias 仅留在 migration 层。回退必须产生结构化 config diagnostic。

### 3.4 输出限制有三套互相覆盖

- Hook：`tool_output.max_chars/max_lines`；
- AgentEvent：500 字符；
- CLI：1200 字符、read 5 行、其他 20 行。

用户调整 `tool_output` 后，CLI 未必体现预期。应区分 model cap、archive policy、UI preview policy。

### 3.5 `session.auto_save` 被加载但运行路径未使用

Config 中存在 `session_auto_save`，但除加载/存储外没有运行引用。退出和 `/new` 仍直接保存，因此该配置看似可控，实际不起作用。

### 3.6 UI 行为与 provider 行为混在模型 profile

reasoning effort、thinking enabled、reasoning replay、inline/quiet display 分散在 profile、LLM runtime 和 Agent session 字段。用户较难区分“发给 provider 的参数”和“本地怎么展示”。

建议拆成：

- `models.profiles.*.reasoning`：provider/request semantics；
- `ui.reasoning_display`：本地展示；
- session override：临时选择，不反向污染 persisted provider config。

## 4. 目标分层

```text
Domain / Runtime
  typed RuntimeEvent + typed CommandResult + typed ToolOutcome
                    |
                    v
Presentation
  EventReducer -> TranscriptModel + RuntimeViewState
  ViewModelBuilder -> Help/Models/Sessions/Approvals/... models
  PresentationPolicy -> compact / standard / debug
                    |
          +---------+---------+
          |                   |
          v                   v
CLI Adapter                TUI Adapter
Rich renderables           Textual widgets/screens
prompt_toolkit input       Textual message/input loop
append-only sink           retained/virtualized transcript
```

### 4.1 RuntimeEvent：描述发生了什么

建议使用可穷举 dataclass union，而不是 `message + data`：

```text
TurnStarted / TurnFinished
AssistantContentDelta / ReasoningDelta
ToolStarted / ToolOutputDelta / ToolFinished
SubagentJobChanged
DiagnosticsPublished / DiagnosticsCleared
ApprovalRequested / ApprovalResolved
NotificationRaised
SessionChanged
RuntimeStateChanged
ViewRequested / ViewRefreshed
```

所有生命周期事件带统一 envelope：

```text
event_id
timestamp
agent_id
session_generation
turn_id
correlation_id (tool_call_id/job_id/request_id)
```

Runtime event 不包含 Rich markup、Textual CSS class 或最终 Panel title。

### 4.2 Presentation reducer：决定它在界面上是什么

Reducer 消费 RuntimeEvent，维护共享 display state：

```text
TranscriptModel
  cells: AssistantCell | ToolCell | DiffCell | DiagnosticCell |
         SubagentCell | NoticeCell | ApprovalCell

RuntimeViewState
  active_session
  model/mode
  token/context usage
  active tools/jobs
  pending approvals
  health/config diagnostics
```

Tool start/end 必须通过 `tool_call_id` 更新同一 ToolCell。找不到 correlation ID 时创建 orphan cell，并记录可观察 diagnostic，不能静默附到最近工具。

### 4.3 PresentationPolicy：统一控制冗长度

不要在每个 renderer 中散落数字和 if。定义共享策略：

```text
verbosity: compact | standard | debug
tool_output_mode: errors | summary | preview | full
max_preview_lines
max_preview_chars
show_tool_args
show_reasoning: hidden | indicator | inline
notification_threshold
```

策略输出“应该展示哪些 DisplayBlock”，CLI/TUI 只负责布局和颜色。

建议默认 `compact`：

- assistant：直接 Markdown，无外框；
- 普通 info/success：单行符号，不用 Panel；
- warning/error/approval：才使用强调块或 modal；
- tool start/end：合并为一条 lifecycle cell；
- read/glob/grep 成功默认只显示摘要；
- shell 显示命令、exit code、duration，输出 head+tail；
- edit/write 显示文件和修改统计，diff 放可展开区域；
- subagent 状态原地更新或只提交最终 cell，不为每个状态打印独立 Panel；
- debug 模式才显示 event kind/source/archive 等内部信息。

### 4.4 Typed ViewModel：命令视图跨 UI 复用

Help、Model、Mode、Session、Approval、MCP、Skills、Token Usage 不应返回任意 dict 或预渲染 Markdown。为每类视图建立 typed model，例如：

```text
ModelListViewModel
  active_main
  active_sub
  rows: list[ModelRow]
  diagnostics
  available_actions
```

CLI presenter 可将它转成 Rich Table/Markdown；TUI presenter 转成 DataTable/ListView。业务 handler 只返回 model 和 effect。

### 4.5 Command use case：只返回 effect

命令 handler 不直接调用 UI bus。统一返回：

```text
CommandEffect
  control: continue | chat | exit
  state_changes
  notifications: list[Notification]
  views: list[ViewRequest]
  interactions: list[InteractionRequest]
```

Dispatcher 是唯一发布者。删除 `open_view()` 与 `view_requests` 双通道。

### 4.6 Interaction port：同步语义与 UI 调度分离

业务侧只产生 typed request；协调器负责等待 response。CLI 可以同步阻塞，TUI 把 request 投递主线程并异步 resolve，但两者共享同一 request model。

Approval diff preview 的构造属于 application presenter/service，不属于 `make_cli_handler`。CLI/TUI 应收到同一个 `ApprovalViewModel`。

### 4.7 UI adapter

CLI adapter 只保留：

- Rich style/theme token 映射；
- Markdown streaming sink；
- prompt_toolkit 输入；
- append-only cell renderer；
- 终端宽度与 ANSI 处理。

TUI adapter 只保留：

- Textual App/message pump；
- transcript widgets 和 virtualization；
- modal/overlay；
- focus、keyboard、resize；
- sidebar/status/composer 布局。

参考 Codex TUI 时可借鉴的不是具体 UI，而是：事件有明确 variant/correlation ID；exec 有独立 model/render；history cell 是稳定展示单元；UI 变更有宽度相关 snapshot。不要复制其当前超大 App/ChatWidget 复杂度。

## 5. 建议包结构

```text
reuleauxcoder/
  presentation/
    events.py             # typed UI/runtime event envelope
    reducer.py            # event -> transcript/state
    transcript.py         # display cell models
    policy.py             # compact/standard/debug
    tool_presenter.py     # ToolOutcome -> ToolCell
    notifications.py
    views/
      models.py
      builders.py
    interactions.py       # typed UI request/response models

  interfaces/
    cli/
      app.py
      event_sink.py
      transcript_renderer.py
      cells/
      views/
      interactor.py
      theme.py
    tui/
      app.py
      event_sink.py
      transcript/
      screens/
      views/
      interactor.py
      theme.tcss
```

是否使用顶层 `presentation/` 或 `interfaces/shared/presentation/` 可以讨论；关键约束是该层不得导入 Rich/Textual，也不得做文件系统或 LLM I/O。

## 6. 配置建议

先建立 typed effective config，再考虑改名。建议概念结构：

```yaml
models:
  active_main: gpt-4.1
  active_sub: gpt-4.1-mini
  profiles: {}

ui:
  verbosity: compact
  reasoning_display: indicator
  tool_output: summary

cli:
  history_file: ~/.rcoder/history
  color: auto

tui:
  sidebar: auto
  mouse: true

lsp:
  enabled: true
  include_warnings: false
  diagnostics_delivery: next_turn

session:
  auto_save: true
```

不要一开始暴露大量颜色、边框和行数配置。先提供少量语义 profile，避免把当前实现细节固化成公共配置。

同时提供 `/config show --effective` 或等价 view，显示：

- effective value；
- 来源：default/global/workspace/session/CLI；
- legacy alias/migration warning；
- 未生效或无消费者的配置诊断。

## 7. 迁移顺序

### Phase 0：视觉基线

- 为当前 CLI 建立 80/120 列 golden/snapshot。
- 覆盖 assistant stream、tool、diff、approval、subagent、notification、views。
- 暂不改样式，先固定行为。

### Phase 1：补齐 typed correlation，不改视觉

- Tool 事件加入 tool_call_id、status、duration、output metadata。
- 引入 ToolOutcome；保留 legacy string adapter。
- UIEvent 增加 typed payload union，旧 message/data 暂兼容。

### Phase 2：共享 presentation reducer

- 把 `_ContentBlock/_ToolCallBlock/_NotificationBlock` 提升为共享 TranscriptCell。
- 把 truncation/summary/diff 判断移出 CLIRenderer。
- CLI 改为渲染 DisplayCell，确保 snapshots 基本不变。

### Phase 3：Command 与 View 解耦

- command handler 改为纯 CommandEffect。
- typed ViewModel 替代 Markdown/dict payload。
- CLI view renderer 全部移动到 `interfaces/cli/views`。
- 删除 bus + return 的双通道。

### Phase 4：CLI 样式和冗长度优化

- 引入 compact/standard/debug policy。
- 普通通知从 Panel 改为单行。
- Tool/subagent lifecycle 合并。
- 修正 shell head+tail、write diff、LSP diagnostics 分段展示。

### Phase 5：TUI shell

- Textual App 使用 queued event sink。
- 先实现 transcript、composer、approval modal、status line。
- Session/sidebar、MCP/Skills/Models views 后续逐步接入。

### Phase 6：配置收敛

- 修 inconsistent defaults 和无效 `session.auto_save`。
- legacy migration 与 effective config diagnostics。
- 最后再决定 LSP 是否改为显式 opt-in，避免无迁移的行为破坏。

## 8. 开工门槛

进入 TUI 实现前，至少应满足：

- CLI renderer 不再直接消费裸 `AgentEvent/UIEvent.data`；
- Tool start/end 有 correlation ID；
- command extension 不再 import `interfaces.cli` 或 Rich；
- command 输出只有一条 effect 通道；
- Help/Models/Sessions/Approvals/MCP/Skills 至少有 typed ViewModel；
- approval 使用共享 ApprovalViewModel；
- presentation reducer 有确定性测试；
- CLI 关键视图有宽度 snapshot；
- TUI 与 CLI 能对同一事件序列生成语义等价 transcript cells。

达到这些条件后，TUI 才是在实现新的 adapter，而不是复制第二套业务前端。
