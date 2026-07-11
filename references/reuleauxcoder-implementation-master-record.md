# ReuleauxCoder TUI 前置改造总实施记录

> 更新时间：2026-07-12
> 状态：v0.4.0 前置改造已实现并通过门禁；本文同时保留原始实施计划与最终总账。
> 用途：作为 CLI/TUI、Subagent/LSP、Extension/Hook、Remote Peer 四条改造线的统一开工入口和实施总账。

## 0. 最终 Handoff

v0.4.0 已完成四份专题文档定义的 TUI 前置架构闭环。TUI 应直接复用 typed RuntimeEvent、PresentationReducer/Policy、typed ViewModel、CommandEffect、InteractionCoordinator 和 CLI 已验证的 scheduler；不得重新引入裸 dict、Rich 业务对象或第二套 Tool/approval/remote 语义。

TS/LSP 最终矩阵为：Python、TS 7 native、TS 6 legacy、JavaScript、YAML、Bash、Go、C、C++、Rust，以及 stale、多文件、parent/subagent 与多 workspace/generation 隔离；Remote workspace 固定不启动 Host LSP。TS 7 通过原生 `tsc --lsp --stdio`，TS 6 兼容链路使用 `typescript-language-server`，由 `lsp.typescript_mode = auto | native | legacy` 控制。

发布收口为 `0.4.0`。全量复验命令与最终结果记录在 `reuleauxcoder-pre-tui-definition-of-done.md`；提交保持按 runtime/presentation、subagent/LSP、extension/peer、protocol/CI、release/docs 分批原子化。

## 1. 已定原则

1. TUI 不是新建一套运行逻辑，而是共享 presentation 内核上的第二个 UI adapter。
2. Tool 的产品语义只在 Host 实现一次；平台差异下沉为 `WorkspacePort` 原语。
3. Remote Peer 只保留 CLI、连接管理、终端能力和远端 OS 原语，不拥有 Agent、Tool、Hook、Session、Approval 或 Markdown 语义。
4. Core Tool Pipeline 的安全、授权、输出规范化和状态一致性不可由可选插件替换；扩展只接窄接口。
5. RuntimeEvent、ToolOutcome、ViewModel、CommandEffect 必须是强类型协议，不再由 renderer 猜字符串和任意 `dict`。
6. Subagent、LSP、Hook、Peer 的每个长生命周期对象必须有 scope、generation、取消和 disposal 语义。
7. 迁移按兼容适配器逐段完成，不做一次性 Big Bang；每段在删除旧路径前必须有 contract/snapshot 测试。

## 2. 四份详细设计的职责

| 文档 | 负责回答 | 实施时的权威范围 |
| --- | --- | --- |
| `reuleauxcoder-cli-tui-architecture-notes.md` | CLI 为什么耦合、presentation 如何共享、配置如何收敛 | RuntimeEvent、Reducer、Transcript、Policy、ViewModel、CommandEffect、InteractionCoordinator |
| `reuleauxcoder-subagent-lsp-handoff.md` | Subagent/LSP 时序和 stale 的来源是什么 | generation、取消、Job 生命周期、工具隔离、LSP 队列/版本/诊断批次/workspace scope |
| `reuleauxcoder-extensions-hooks-peer-notes.md` | Hook/Plugin 如何收口、Peer 如何变薄 | Core Pipeline、ExtensionManager、scope/disposal、RemoteTerminalSink、WorkspacePort、协议可靠性 |
| `reuleauxcoder-pre-tui-definition-of-done.md` | 什么时候才允许开始 TUI | 全部验收门槛和测试要求 |

发生冲突时，以 Definition of Done 的验收约束为准；其余三份文档分别解释设计理由和实现边界。

## 3. 必须遵守的依赖顺序

```text
Typed RuntimeEvent + ToolOutcome
              │
              ▼
PresentationReducer + TranscriptModel + PresentationPolicy
              │
       ┌──────┴────────┐
       ▼               ▼
CLI adapter      CommandEffect / Typed ViewModel
       │               │
       └──────┬────────┘
              ▼
InteractionCoordinator + Local/Remote TerminalSink
              │
       ┌──────┴──────────┐
       ▼                 ▼
Subagent/LSP scope   Thin Remote Peer
       │                 │
       └──────┬──────────┘
              ▼
Fixed Core Pipeline + ExtensionManager
              │
              ▼
WorkspacePort local/remote migration
              │
              ▼
Config cleanup + architecture/contract/full-suite gate
```

不能先写 TUI shell：否则 CLI 字符串协议、命令 Rich 对象、重复的交互调度和远端渲染会被原样复制进 TUI。

## 4. 工作包与明确交付物

### WP-1：强类型运行协议

- 引入带 `event_id`、`session_id`、`turn_id`、`correlation_id`、时间戳和 typed payload 的 `RuntimeEvent`。
- Tool start/end 使用稳定的 `tool_call_id` 配对，支持并行、乱序完成和重放。
- 引入 `ToolOutcome`，分别保存模型可见文本、展示摘要、结构化 diff/diagnostic/artifact、截断元数据和错误分类。
- 删除领域事件层固定 500 字符截断；限制只由 outcome policy 和 presentation policy 各自负责。
- 保留 legacy adapter，直到 CLI、Hook、Peer 和测试全部迁移。

### WP-2：共享 Presentation 内核并迁移 CLI

- 建立纯逻辑 `PresentationReducer`、`TranscriptModel`、`RuntimeViewState`、`PresentationPolicy`。
- reducer 不依赖 Rich、Textual、prompt_toolkit、LLM client 或网络 I/O。
- CLI 只把 typed cell/view state 渲染到终端，不再探测工具名、`---`、`[truncated]` 或私有 data key。
- 默认样式降低 panel 密度；通知、工具调用、子任务结束只保留一个权威展示通道。
- transcript 有容量策略；后台事件统一通过 UI scheduler 串行进入 renderer。

### WP-3：命令与交互解耦

- Command handler 只返回一个 `CommandEffect`，不再同时发布 EventBus 和返回 view request。
- Sessions、MCP、Config 等命令返回 typed ViewModel，不构造 Rich `Panel`、`Table` 或 CLI helper。
- `InteractionCoordinator` 统一 approval、selection、text input、cancel、deadline 和前台输入占用。
- Local CLI、未来 TUI、Remote CLI 只实现各自 interaction/terminal adapter。

### WP-4：Subagent 生命周期收口

- Job 必须先登记再提交，消除 fast-completion callback 竞态。
- 每个 session/reset/new 建立 generation；旧 generation 的结果不得注入新会话。
- 区分 requested-stop、cancelled、timed-out、failed、completed；timeout 不能伪装成功。
- 子 Agent 不共享有可变状态的 Tool 实例；cwd、approval、Hook/LSP scope 独立或显式只读共享。
- Manager 提供 shutdown、join、prune 和 pending-injection 清理。
- 后台完成事件只进入 RuntimeEvent 通道，不直接调用 Rich renderer。

### WP-5：LSP stale 与 scope 收口

- worker 每轮只 pop 实际执行的队列项，或逐项处理；不得同时 pop 后丢弃两个任务。
- `didChange` 版本按 document 单调递增。
- `publishDiagnostics` 按 URI/version/batch 替换；空列表必须能清掉旧诊断。
- manager 保存 clean 结果，消费使用 batch token/seq，不再用全局 `_diagnostics_fed` bool。
- observer 只消费与当前 tool outcome、workspace、document、generation 相关的诊断。
- LSP 实例按 workspace root + language + session scope 管理，并尊重配置的 command override。
- 远端 workspace 没有一致文件视图时，Host LSP 不得读取本地同名路径冒充远端诊断。

### WP-6：Core Pipeline 与 Extension/Hook

- 固定执行顺序：参数规范化 → workspace 解析 → 授权 → 执行 → outcome 规范化 → context/diagnostic 增强 → observer → runtime event。
- 将旧 Hook 收敛到窄接口：`AuthorizationPolicy`、`ContextContributor`、`OutcomeProcessor`、`RuntimeObserver`、`LifecycleParticipant`。
- 引入带 API version、依赖、ordering constraints、config namespace、scope policy 的 `ExtensionManifest`。
- `ExtensionManager` 负责 discovery、拓扑排序、实例化、scope container 和反向 disposal。
- 核心安全规则 fail closed；可选 observer 故障可降级但必须产生结构化诊断。
- 禁止通过模块全局 registry、浅 `copy.copy` 或私有字段注入跨 session 状态。

### WP-7：Remote CLI 与协议

- Host remote handler 复用和本地 CLI 相同的 presenter，只替换 `RemoteTerminalSink`。
- Peer 不再用 glamour 解释 Markdown，不维护产品 Tool 语义，不重做 approval diff。
- Peer 负责 terminal capabilities、stdin/stdout、auth、heartbeat、long-poll、重连退避、cancel/deadline。
- Host 校验 capabilities；token TTL 配置真正下发，并支持过期前续签。
- poll 支持无事件阻塞、瞬时故障重连和 session resume；协议提供 request/event/cancel 的稳定 ID。
- Go 端保留的原语必须有单测；Python/Go schema 必须有双向 contract tests。

### WP-8：WorkspacePort

- Host Tool 只依赖平台无关原语，例如 `fs.stat/read/write_atomic/replace_exact_atomic/list/search` 和 `process.start/input/cancel`。
- `LocalWorkspacePort` 和 `RemoteWorkspacePort` 实现同一契约；路径约束、错误码、编码、原子性一致。
- `read/write/edit/glob/grep/shell` 的产品语义只存在于 Host Tool 层。
- 迁移期旧 `exec_tool` 走兼容 adapter；所有工具迁移后删除 Peer 端重复实现。
- 远端路径必须限制在协商 workspace root，拒绝绝对路径逃逸和 `..` 越界。

### WP-9：配置与可诊断性

- 明确 LSP 默认启用策略，修复 `include_warnings` 在“无 section/有 section”时默认值不一致。
- 模型激活字段收敛到一个规范入口；无效 profile/alias 报错而非静默回退。
- tool output 只保留模型上下文限制与 presentation 展示限制两层，打印 effective config。
- `session.auto_save` 要么接入统一生命周期，要么移除。
- provider 能力与 UI reasoning 展示策略分开配置。
- 提供可脱敏的 diagnostics 输出：effective config、extension graph、LSP scopes、peer capabilities、active jobs。

## 5. 测试门禁

每个工作包必须同时补测试，不接受“代码完成后统一补”。最低门禁如下：

| 层级 | 必测内容 |
| --- | --- |
| Architecture fitness | command/extension 不导入 CLI/Rich/Textual；presentation core 不导入 UI framework/LLM I/O；Peer 不拥有产品 Tool |
| Runtime/presentation | event correlation、并行乱序、重复事件去重、bounded transcript、verbosity snapshots、窄终端宽度 |
| Subagent | fast completion、reset generation、timeout/cancel、异常传播、shutdown、Tool/Hook/LSP 隔离 |
| LSP unit/concurrency | 混合队列不丢任务、版本递增、diagnostic replace/clear、batch scope、workspace isolation |
| LSP integration | broken → fixed → broken；Python/YAML/Bash/Go/C/C++，TS/Rust 在依赖满足时纳入硬门禁 |
| Extension | dependency/order cycle、scope clone、failure policy、reverse disposal、lifecycle exactly-once |
| Peer Go | path confinement、capability enforcement、token renewal、poll backoff/resume、cancel/deadline、terminal resize |
| Cross-language | handshake、envelope/schema、错误码、WorkspacePort 原语、兼容版本拒绝/降级 |
| Regression | Python 全量测试、Go 全量测试、CLI golden/snapshot、真实 local/remote smoke |

当前已知基线仅用于比较，不代表上述门禁已经完成：

- Python 基线曾通过 `536 passed, 18 skipped`。
- Go `go test ./...` 可编译，但当前包普遍没有测试，不能视为 Peer 已验收。
- 真实 LSP smoke 曾为 `6 passed, 3 failed, 1 skipped`；TS/JS 临时 workspace 缺 TypeScript 安装，Rust 未返回期望诊断。这些需要区分环境前置条件与真实实现缺陷后再设 CI 门禁。
- 运行测试时需要清理环境中的 SOCKS proxy 干扰；如测试必须绑定本地 socket，可使用已获准的提权测试。

## 6. TUI 开工前的硬性完成定义

只有以下各项全部为真，才允许开始 TUI shell：

- [x] CLI 已完全消费共享 RuntimeEvent、Reducer、Transcript 和 Policy。
- [x] Command 不再返回 Rich/CLI 对象，交互由 Coordinator 统一调度。
- [x] 同一录制事件流可分别驱动 CLI adapter 和 headless test adapter，语义一致。
- [x] Subagent 的 generation、取消、隔离、shutdown 测试通过。
- [x] LSP 的队列、版本、replace/clear、batch/workspace scope 测试通过。
- [x] Core Pipeline 与 ExtensionManager 的 scope/disposal/ordering 测试通过。
- [x] Remote CLI 使用共享 presenter，Peer 已移除重复 Tool/Markdown/approval 语义。
- [x] Local/Remote WorkspacePort contract 测试通过，路径逃逸被拒绝。
- [x] 配置默认值一致，effective diagnostics 可解释实际运行状态。
- [x] Python、Go、跨语言 contract、CLI snapshot 和真实 smoke 全部通过。

任何未勾选项都属于 TUI 的前置架构债，不能转成“TUI 实现时顺手处理”。

## 7. 实施状态总账

| 工作包 | 调研 | 设计 | 实现 | 测试 | 备注 |
| --- | --- | --- | --- | --- | --- |
| WP-1 RuntimeEvent / ToolOutcome | 完成 | 完成 | 完成 | 完成 | typed codec、correlation、结构化 projection |
| WP-2 Presentation / CLI | 完成 | 完成 | 完成 | 完成 | shared reducer/policy、宽度 snapshots |
| WP-3 Command / Interaction | 完成 | 完成 | 完成 | 完成 | 单一 effect、typed views、generic interaction |
| WP-4 Subagent | 完成 | 完成 | 完成 | 完成 | generation、登记、取消、隔离、cleanup |
| WP-5 LSP | 完成 | 完成 | 完成 | 完成 | stale/batch/root/remote 与真实语言矩阵 |
| WP-6 Extension / Hook | 完成 | 完成 | 完成 | 完成 | fixed pipeline、scope/disposal/ordering |
| WP-7 Remote Peer | 完成 | 完成 | 完成 | 完成 | 薄 Peer、可靠连接、Go tests/发布门禁 |
| WP-8 WorkspacePort | 完成 | 完成 | 完成 | 完成 | Host 语义 + local/remote fs/process 原语 |
| WP-9 Config / Diagnostics | 完成 | 完成 | 完成 | 完成 | effective state、migration diagnostics |

状态更新规则：只有实现合入且对应门禁测试有可复现命令和结果时，才可将“实现/测试”改为完成。

## 8. 第一批可直接开工的变更

第一批只做建立后续迁移支点且不改变 CLI 视觉的工作：

1. 增加 typed `RuntimeEvent` envelope 和 legacy `AgentEvent/UIEvent` adapter。
2. 为 tool lifecycle 增加稳定 `tool_call_id`，删除 500 字符领域截断。
3. 增加 `ToolOutcome` 及 legacy string adapter。
4. 建立 headless `PresentationReducer`/`TranscriptModel` 骨架与乱序、去重、容量测试。
5. 增加 architecture fitness test 的允许列表，先记录现有违规，再随迁移逐项归零，禁止新增违规。

这一批完成后再迁移 CLI renderer；不要在 typed event 尚未稳定时同时修改终端样式，否则很难判断回归来自语义还是视觉。

## 9. 维护约束

- 本记录是总账，不替代四份详细文档；实现细节回写对应专题文档，状态统一回写本记录。
- 新发现的问题先判断是否破坏既定边界；若会改变依赖方向或公开协议，必须先更新设计和 contract test。
- 不以“Peer 也能处理”“TUI 以后再抽”“Hook 更灵活”为理由复制核心语义。
- 不以测试环境缺依赖掩盖实现问题；环境缺口必须显式 skip 并说明安装前置，逻辑缺陷必须失败。
- 所有兼容 adapter 都必须记录删除条件；没有删除条件的临时层会成为永久维护债。
