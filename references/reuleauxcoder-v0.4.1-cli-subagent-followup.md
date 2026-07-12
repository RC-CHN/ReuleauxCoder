# ReuleauxCoder v0.4.1 CLI / Subagent Follow-up

> 日期：2026-07-12  
> 状态：设计确认中；本文记录本轮截图验收暴露的问题与拟实施方案。除已经提交的 CLI 滚动、review 主区迁移和 transcript 时序修复外，本文后续项尚未编码。

## 1. 本轮已经确认的问题

### 1.1 Tool completion 会泄漏 `None`

mini-TUI 当前直接插值 `ToolOutcome.summary`。LSP 仍有 legacy string result，转换后的 `summary` 为 `None`，于是出现：

```text
SUCCEEDED  lsp · None
```

这不是 LSP 结果为空；完整结果仍返回给 Agent。问题是 ToolOutcome 的结构化摘要不完整，UI 又没有合法兜底。

### 1.2 mini-TUI 没有真正渲染 Assistant Markdown

主区的 `AssistantCell` 当前作为纯文本输出，因此：

- `**bold**` 原样显示；
- Markdown table 的 `|`、分隔行原样显示；
- code、list、heading 等与 append-only v0.4 CLI 的 Rich Markdown 风格不一致；
- 用户会把 Markdown pipe 与滚动 stale 残留混在一起判断。

### 1.3 滚动存在 stale glyph、卡顿和不稳定重排

当前 mini-TUI 使用 prompt_toolkit alt screen、`ScrollablePane`、完整 retained transcript 和每帧格式化文本重建。截图中出现左侧残余竖线/字符，滚动体感也偏卡。

主要风险点：

1. 每次 repaint 都遍历全部 transcript cells；
2. resize、stream chunk 和滚动可能重复做历史内容折行/测量；
3. `ScrollablePane` 先画大型虚拟画布再复制 viewport，长 transcript 成本随历史增长；
4. 宽字符、emoji、边框和 scrollbar 占位变化会放大虚拟画布与真实 screen diff 的 stale；
5. 用滚动时全屏 `clear()` 只能遮住 stale，却会引入闪烁和更差性能，不作为正式方案。

### 1.4 子 Agent 的内部输出错误地穿透到主 transcript

子 Agent 复用 root event transport 是正确的，但 mini-TUI 当前把 transport 可见性误当成用户可见性：child assistant chunks、tool rows、tool output、completion 可能进入主对话区。

正确边界是：

- 所有 child runtime events 继续进入 ledger、session replay facts 和 Execution reducer；
- 顶部 Agent 面板展示 child 的任务、状态、简短 progress 和 activity pulse；
- child 的详细回复、工具参数、实时输出和最终结果默认不进入用户 transcript；
- child result 只回注直接父 Agent，由父 Agent 决定如何向用户总结；
- child 触发人工审批时，REVIEW 仍必须进入前台，这是授权边界，不是普通输出。

### 1.5 子 Agent 当前实际仍有递归委派入口

当前 `run_subagent_task()` 调用：

```python
_filter_subagent_tools(parent_agent, mode, include_agent=True)
```

这会把 `agent` 工具交给 child，与本轮确认的产品边界冲突。

最终决策：子 Agent 不持有 `agent` 工具，禁止递归委派。旧 session/ledger 中的 `depth` 继续用于兼容、审计和拒绝非法恢复，但不代表允许运行时创建孙级 Agent。

### 1.6 Plan ownership 需要收紧

全局 Plan 是 root Agent 对用户任务的唯一 checklist 和承诺。child 当前会获得 `update_plan`，存在多个写入者、revision 竞争、动态上下文抖动和错误覆盖风险。

最终决策：

- 只有 root Agent 可以持有 `update_plan`；
- child 保留窄职责的 `report_progress`、`report_to_parent`、`request_guidance`，但不持有全局 Plan 写能力；
- child 发现全局计划需要改变时，通过 mailbox 或最终结果建议父 Agent；
- 父 Agent自行判断并更新全局 Plan。

### 1.7 子 Agent 工具 `reason` 不能一刀切

本轮先提出“所有 child tool 都增加 reason”，随后明确收窄：幂等只读工具不需要机械解释每一次调用。

最终规则：

| Child tool 类别 | `reason` | 说明 |
|---|---:|---|
| `read_file`、`list_file`、`glob`、`grep` | 不要求 | 所有 child mode 默认持有；幂等只读、免人工审批 |
| 查询型 `lsp` | 不要求 | 所有 child mode 默认持有；当前三个 operation 均免人工审批 |
| `report_progress` | 不要求 | 参数本身已经表达目的 |
| `report_to_parent` | 不要求 | message/kind/reply_to 本身表达通信目的 |
| `request_guidance` | 不要求 | question/context 本身表达阻塞原因 |
| `write_file`、`edit_file` | 必填 | 修改工作区，需要可审计意图 |
| `shell` | 必填 | 无法仅靠 command string 稳定判断副作用/成本 |
| `agent` | 不提供 | child 禁止递归委派 |
| `update_plan` | 不提供 | 全局 Plan 仅 root 可写 |

这里同时形成一条显式的 child-scope 授权规则：`read_file`、`list_file`、`glob`、`grep` 与当前查询型 `lsp` 默认放行，不进入人工审批；其余工具仍继承父 Agent 的审批策略。该豁免必须在 scoped authorization 层按明确 capability/effect class 实现，不能借“缺少 reason”隐式推导。

当前 `lsp` 只允许 `goToDefinition`、`findReferences` 和 `documentSymbol`。如果以后增加 `rename`、`codeAction`、format/apply edit 等可修改工作区的 operation，必须拆成新的 effectful capability/tool，不能继承查询型 `lsp` 的免审批规则。

### 1.8 Subagent 创建与等待采用 Codex 异步模型

最终决策：root 的 subagent 创建操作只保证 job/session 已经注册并开始调度，随后立即返回稳定 job ID；它永远不等待 child 完成。删除面向模型的 `run_in_background`、`awaited`、`detached` 三套重叠语义，也删除父 loop 在普通 assistant response 结束后因存在所谓 awaited job 而自动等待的行为。

root 单独持有 `wait_agent`。它等待的是 subagent mailbox activity，而不是在 spawn 调用内部 join 某个 worker：

- mailbox 已有未消费 activity 时立即返回；
- 否则等待任一 child 的 reply、completion、blocked/attention 等有效 activity；
- 人类 steering 可以中断等待，使 root 优先处理用户输入；
- 超时只结束本次等待，不取消 child；
- `Ctrl+C`/显式 cancel 与 wait timeout 分开，只有明确取消操作才改变 child 生命周期；
- `send_message` 仍只负责可靠投递，绝不隐式等待回复。

这里必须区分两件事：child 在独立 worker 上物理异步运行；root 是否暂时停下来等新 activity，是由模型显式调用 `wait_agent` 决定。简单任务的正常路径是 `spawn -> wait_agent -> consume result`，可并行任务则是 `spawn -> continue useful work -> wait_agent`，不再需要额外的前台/后台模式。

### 1.9 Child 的正常结束与强制停止

child 正常结束不增加专门的 finish tool。模型返回一个没有 tool calls 的最终 assistant message，即表示本次 child invocation 完成；manager 将最终消息与可信 runtime facts 合成为 `SubagentResult`，写入 transcript store 和 parent mailbox。

delegated prompt 必须明确要求最终消息按以下顺序给出，允许纯文本或等价结构，但不得省略未知/未完成项：

1. **结论**：直接回答 delegated task；
2. **证据**：实际读取、命令、测试或诊断所得证据；
3. **修改与产物**：变更文件、artifact、worktree 状态；没有则明确写无；
4. **未解决问题**：阻塞、风险、待父 Agent判断事项；没有则明确写无；
5. **置信度**：high / medium / low，并给出降低置信度的原因。

`report_progress`、`report_to_parent` 和 `request_guidance` 都不是完成信号。manager 不完全相信 child 自报：tool ledger、workspace change facts、失败 outcome、budget/timeout 和 transcript reference 仍由运行时补齐或纠正。

root 与人类都必须能够查询和强制停止 child。这里的“强制停止”是成本与副作用保证，不是只把 UI 状态改成 cancelled：

- 原子地把 job 切到 `cancelling`，禁止发起任何后续模型轮次或工具调用；
- 立即触发当前 LLM request 的 cancellation handle，并关闭 streaming/HTTP response，使远端停止继续生成；
- 向正在运行的 cancellable tool 传播 cancellation；shell/peer 等进程工具终止整个进程组，而不只杀 wrapper；
- approval、guidance wait 和 mailbox wait 立即解除并关闭对应 request；
- 经过短 grace period 仍未退出的 child worker 必须被硬终止；要实现这一保证，child 执行边界不能依赖不可安全杀死的共享 Python thread，必要时使用独立 worker process；
- terminal 状态写为 `cancelled`/`killed` 并保存 partial transcript、已发生费用和工具事实；
- cancellation epoch 之后到达的 provider chunk、tool result、reply 和 completion 全部隔离，不能写入父上下文、恢复 job 或继续修改工作区。

关闭连接不能追回取消前已经生成的 token，但必须阻止可避免的后续生成。若 provider/transport 无法提供可验证的 request cancellation，该 backend 不满足 subagent 强制停止能力，必须 fail closed 或在可硬杀的独立进程中运行，不能静默退化为后台失控计费。

## 2. 目标架构

### 2.1 Runtime 可观察，Transcript 有路由

```text
all RuntimeEvents
        |
        +--> HistoryLedger / session facts       全量保留
        +--> ExecutionViewReducer                root + child 活动
        +--> Attention / Approval coordinator    授权事件
        `--> Transcript router
               +--> root conversational events  用户主区
               +--> child approval request      用户 REVIEW
               `--> child internal events        默认隐藏
```

不能在事件源上停止 child 事件传播，否则会破坏状态面板、审计和恢复；应在 presentation projection 处明确路由。

### 2.2 Root/child 工具能力矩阵

```text
root Agent
  spawn_agent, send_message, wait_agent, list_agents, interrupt_agent,
  update_plan, report_progress, normal mode tools

child explore
  read_file, list_file, glob, grep, lsp(query)
  report_progress, report_to_parent, request_guidance

child execute
  read_file, list_file, glob, grep, lsp(query), write_file(reason),
  edit_file(reason), shell(reason)
  report_progress, report_to_parent, request_guidance

child verify
  read_file, list_file, glob, grep, lsp(query), shell(reason)
  report_progress, report_to_parent, request_guidance
```

五个查询能力 `read_file`、`list_file`、`glob`、`grep`、`lsp(query)` 是所有 child mode 的共同基线。其他能力仍必须由 mode allowlist 明确授予；本决策不扩大 write/shell/control 权限。

### 2.3 Scoped schema 注入，不污染工具原语

`reason` 应由 subagent scoped-tool materialization 层注入：

1. clone 独立 tool/backend；
2. 根据 effect class/name 为 child schema 增加必填 `reason`；
3. Tool start event、BeforeToolExecuteContext、approval metadata 和 ledger 保留 reason；
4. 在调用具体 tool implementation 前剥离 reason；
5. workspace/process 原语及 root tool schema 不增加该字段。

这样不会逐个修改 read/edit/shell 的平台实现，也不会把 UI/编排元数据误传给远端 primitive。

### 2.4 Child 三个控制工具

三个工具必须保持不同的信息流与阻塞语义：

| 工具 | 主要接收者 | 父上下文 | 阻塞 child | 用途 |
|---|---|---:|---:|---|
| `report_progress` | 人类 Execution Panel | 否 | 否 | 低频状态与下一步 |
| `report_to_parent` | 直接父 Agent | 是 | 否 | reply、milestone、amendment、warning |
| `request_guidance` | 父 Agent + Human Attention | 是 | 是 | 无法安全继续时请求决策 |

`report_progress` 不再提供含义模糊的 `phase=blocked`。普通状态不应进入父上下文，否则 child 的每次进度更新都会破坏父 Agent 的最长缓存前缀。

`report_to_parent` 使用 typed mailbox，至少携带 `message_id`、`reply_to`、`kind`、sender/recipient、job、generation、sequence、content hash。主 Agent 通过 root-only `send_message` 询问 child；人类通过 `/agents message` 走同一个 directive channel。child 收到 directive 后可用 `report_to_parent(kind=reply, reply_to=directive_id)` 精确回复。

`request_guidance` 不在 worker thread 内长期 wait，而是 checkpoint-and-park：完成当前 tool call/result 邻接、持久化 transcript/checkpoint、把 job 标为 `blocked`、释放并发槽；收到父 Agent 或人类的定向 guidance 后，再从同一 job/transcript 恢复。

### 2.5 Root 的异步生命周期控制

root 的 subagent 控制面保持职责分离：

| 操作 | 是否等待 child | 结果 |
|---|---:|---|
| `spawn_agent` | 否 | child identity/job ID；注册/调度失败才同步报错 |
| `send_message` | 否 | directive 已接受或明确拒绝 |
| `list_agents` | 否 | 当前可见 child 的稳定状态快照 |
| `interrupt_agent` | 有界等待强停完成，不等待正常完成 | cancellation ID 与 `cancelled`/`killed` 终态 |
| `wait_agent` | 是，可被人类打断 | mailbox activity / steered / timeout |

`wait_agent` 第一版遵循 Codex 的 activity wait，而不是 request/reply RPC：不要求模型准确维护一组 future，也不把目标 child 的最终文本复制到 wait tool result。child reply、completion 和 blocked 状态先 ledger-first 写入 typed mailbox；父 Agent只在 provider-safe boundary claim、注入和 ack。这样无论 root 是主动等待、继续调用其他工具，还是被人类 steering 打断，事实来源和缓存影响都只有一份。

Execution Panel 始终独立更新，因此 root 等待时 UI 和人类控制面仍然活跃。`wait_agent` 不占 subagent worker slot，也不能持有 session/history 写锁。

`list_agents` 的紧凑结果至少包含 agent/job ID、task 摘要、`queued/running/blocked/cancelling/terminal` 状态、当前阶段/工具、最后 activity、运行时间、预算消耗和 blocker；详情与 transcript 通过显式 inspect 路径获取，避免每次查询把大量 child 内容注入 root context。人类默认从常驻 Agent Panel 获得同一 reducer 的状态，也可用 `/agents` 查询、`/agents stop <id>` 强制停止；root 与 human 必须走同一个 manager cancellation primitive，不能形成两套语义。

`interrupt_agent` 不能在 worker 仍可能继续请求模型时返回一个容易被误解为成功的结果。它先持久化 cancellation epoch 并触发 cooperative abort，在很短的固定 grace period 内等待退出；仍未退出则由 supervisor 杀掉 worker process，最终只以 `cancelled` 或 `killed` 返回。UI 可以在这段有界窗口展示 `cancelling`，但工具成功结果必须代表消费和副作用通道已经被切断。

模型可见 API 直接采用职责单一的 Codex 风格工具名。`spawn_agent` 每次只创建一个 child，参数是一项明确 task/message 及可选 context/model/budget；需要并行时，root 在同一 assistant response 中发出多个独立 `spawn_agent` tool calls，由工具执行层并发创建。删除 `tasks=[...]` batch、`action=`、`run_in_background` 和 `detached` 组合。旧 `agent` schema 仅供旧 replay/session migration 翻译，不能继续注入新模型请求，也不能作为 registry fallback 绕过 root-only 控制工具。

### 2.6 可硬停的 Child Worker 与 Tool Broker

为了同时满足强停、审批一致性、共享 LSP 和远端 peer 复用，目标执行边界是：

```text
root process / SubagentSupervisor
  - job registry + ledger + mailbox + cancellation epoch
  - approval/attention coordinator
  - scoped Tool Broker
      - read/list/glob/grep/lsp
      - write/edit/shell
      - remote peer forwarding
  - event reducer / transcript router
             ^ typed IPC
             |
child worker process
  - isolated Agent messages/context
  - child LLM client + streaming connection
  - model loop and child-only control-tool adapters
  - no direct workspace primitive, no direct global Plan/Agent registry
```

child worker 只拥有模型循环和隔离上下文。普通工作区工具调用通过 typed IPC 交给父进程 Tool Broker；broker 以 job capability、mode、reason、approval token、generation 和 cancellation epoch 重新校验后，调用与 root/remote peer 相同的工具路径。这样不会为了 process isolation 复制一套 LSP server、审批逻辑或文件/shell 实现。

每个活跃 child invocation 独占一个 worker process；不同 job 不在同一进程内多路复用。supervisor 可以限制并发数并复用启动模板，但不能让 hard kill 一个 job 时连带终止其他 child。进程必须使用干净的 spawn/rehydrate 边界，不能 fork 继承父进程的 LSP socket、锁、HTTP client 或审批状态。

控制工具分两类：`report_progress`、`report_to_parent`、`request_guidance` 在 child 侧形成 typed control event，由 supervisor 持久化；工作区工具全部经 broker。worker 不接收可调用的 `agent`、`update_plan` 或未经 scoped materialization 的全局 registry。

强停顺序为：

1. supervisor ledger-first 写入 cancellation epoch，并撤销 job capability；
2. IPC 通知 worker 取消当前 LLM request，Tool Broker 拒绝新调用；
3. broker 取消尚未执行的 approval/tool request，并终止运行中的 shell/peer 进程组；
4. grace period 后 worker 未退出，supervisor hard kill worker process；
5. drain 并丢弃 epoch 之后的 IPC 帧，提交 partial transcript 与 terminal facts；
6. 发布唯一 terminal activity，唤醒 `wait_agent` 和 Agent Panel。

worker crash、OOM、kill 或 IPC 断开都必须由 supervisor 收敛成 terminal `failed`/`killed`，不能留下继续计费但 registry 显示 stale 的孤儿。父进程退出时先撤销全部 child capability，再关闭/杀掉 worker；不允许 child 脱离 session 独立存活。

预算也是 supervisor 的硬边界，而不只依赖 child 自觉：每个 provider request 的最大输出必须被剩余预算约束；每轮实际 usage 回传校准累计值；超过 round/tool/token/wall-clock 任一上限即走同一强停路径。取消请求缺少最终 provider usage 时保留“已知实际值 + 未决尾部”审计字段，不能伪造精确费用。

### 2.7 Worker IPC、Tool Result 与 Checkpoint 契约

IPC transport 可以替换，但 envelope 语义必须固定且 JSON 可序列化。每帧至少携带 `type`、`message_id`、`job_id`、`agent_id`、`session_generation`、`worker_generation`、`cancellation_epoch`、单调 `sequence`、payload hash。supervisor 是 job 状态、capability、approval、mailbox 和 terminal 的唯一权威；worker 自报状态只能作为输入事件。

主要消息方向：

```text
supervisor -> worker
  Start(replay envelope, tool schemas/hash, model settings, budgets)
  ToolResult(call_id, outcome/ref)
  Directive(directive_id, content)
  Cancel(cancellation_id, epoch, reason)
  ParkAck(checkpoint_id)

worker -> supervisor
  Ready(worker_generation, schema hash)
  RuntimeEvent(stream/reasoning/tool/activity)
  ToolRequest(call_id, name, arguments, reason, capability)
  ControlEvent(progress/report/guidance)
  Checkpoint(checkpoint_id, replay envelope/hash, remaining budgets)
  Terminal(final response/checkpoint, usage, status)
```

spawn 的 ledger-first 顺序为：分配 job/agent ID 与 epoch → 持久化 job、父上下文投影、配置和 capability → 启动 worker → 完成 IPC 握手并进入 `running`。`spawn_agent` 在注册和调度成功后即可返回，不等待 Ready，更不等待 Terminal；启动失败作为该 job 的 terminal activity 回报，不能让瞬时失败丢失 identity。

Tool Broker 以 `(job_id, worker_generation, cancellation_epoch, call_id)` 幂等处理请求。重复请求返回同一已提交 outcome，不能重复执行副作用。worker 崩溃时：

- broker 已提交 result：恢复后可重发同一 tool result；
- request 尚未开始：安全取消；
- 副作用已经开始但没有可信 outcome：标为 `indeterminate` 并进入 Attention，不自动重试 write/edit/shell；
- 查询型只读调用可以在 generation/epoch 仍有效时重新执行，但结果仍以新的 attempt 留痕。

工具原始输出由 broker 全量归档；模型可见内容按 tool 声明的截断方向和预算生成。小结果直接内联 IPC，大结果使用 content-addressed `ToolResultRef(path, hash, bytes, model_view_hash)`，worker 校验 hash 后读取模型视图。UI 的 shell 五行实时窗口只消费 RuntimeEvent，不参与模型 result；因此 UI 丢帧、折叠或限流不影响 child 获得的模型可见全文。

Checkpoint 只允许在 provider-safe boundary 提交，必须包含下一次请求可精确重放的 messages、tool schemas、model settings、usage/budget counters、tool adjacency 状态和动态 workspace/LSP generation。保存使用 JSON 原子替换并附 content hash。park/resume 不重建旧消息、不重新总结 child transcript；恢复时原样加载稳定前缀，只把 guidance/directive 作为新的结构化尾部追加，从而保持最长前缀缓存和 provider 邻接。

背压规则也必须明确：stream/reasoning/activity 可以合并或限频后投影 UI，但 ledger-critical event、ToolRequest/Result、Checkpoint、ControlEvent 和 Terminal 不得丢弃。控制帧使用独立优先通道，避免大量 shell 输出阻塞 Cancel 或 approval resolution。

## 3. 拟实施方案

### Phase A：先修语义和可见性

1. 给 mini-TUI/append-only presenter 增加 root transcript routing。
2. child 普通事件只投影到 Execution reducer；过滤 `SubagentJobChanged` 的主区 cell。
3. child approval request/resolution 仍可投影到 REVIEW，并保持 request-id correlation。
4. LSP 返回结构化 `ToolOutcome(content=完整结果, summary=紧凑摘要)`。
5. 所有 Tool completion presenter 使用统一摘要函数，绝不显示字符串 `None`。

### Phase B：收紧子 Agent 能力

1. 删除 `_filter_subagent_tools(... include_agent=True)` 的递归入口，并移除无意义参数。
2. child allowlist 删除 `agent` 与 `update_plan`，保留 `report_progress`。
3. 为所有 child mode 加入只读基线：`read_file`、`list_file`、`glob`、`grep`、查询型 `lsp`。
4. 在 child scoped authorization 层显式放行上述五类查询，不创建人工 approval request。
5. 在 scoped-tool wrapper/schema view 中仅给副作用工具注入必填 `reason`。
6. reason 进入 event activity、approval summary 与 ledger；执行前剥离。
7. 为 child materialize `report_progress`、`report_to_parent`、`request_guidance` 三个窄控制工具，不恢复多功能 `agent`。
8. 将 parent→child mailbox 从 `list[str]` 升级为保留 directive ID 的 typed directive；reply 必须可关联。
9. 实现 blocked job 的 checkpoint/park/resume 状态机与预算、取消、恢复规则。
10. 将 root subagent spawn 收敛为单任务 create-and-return，新增 root-only `wait_agent` activity wait；移除公开的 batch、`run_in_background`/`awaited`/`detached` 与父 loop 自动等待分支。
11. 将 root 控制面拆为 `spawn_agent`、`send_message`、`list_agents`、`interrupt_agent` 和 `wait_agent`；旧 `agent` 只做 replay migration，不再对模型可见。
12. 将 LLM transport、tool execution、approval/guidance wait 与 worker process 接入同一个 cancellation scope，提供可验证的强停和 late-result quarantine。
13. 在 delegated prompt 加入固定最终输出契约，并由 manager runtime facts 校验/补全 `SubagentResult`。
14. 恢复旧 session 时遇到 child 发起 `agent`/`update_plan`，按 unavailable tool fail closed，不补发能力；旧后台参数只做输入迁移，不恢复旧等待语义。

### Phase C：补齐 mini-TUI Markdown

1. 复用现有 Markdown block commitment 规则，不在每个 token 上盲目解析未闭合 block。
2. completed assistant cell 使用与 v0.4 append-only CLI 一致的 Rich Markdown 语义；active cell 只渲染 committed prefix，pending tail 使用普通文本。
3. Markdown 结果转换为 prompt_toolkit fragments，保持 alt-screen ownership，不直接向 stdout 写 ANSI。
4. table 在足够宽时格式化为表格；窄终端允许降级为紧凑行，但不能泄漏 Markdown delimiter。
5. 缓存键至少包含 `(cell id, revision, viewport width, theme revision)`；resize 只失效受宽度影响的 layout cache。

### Phase D：替换高成本滚动热路径

目标不是“滚动时全量清屏”，而是 retained + virtualized viewport：

1. transcript reducer 继续保存 bounded typed cells；
2. 为每个 cell 缓存已经格式化的 fragments、视觉行数和 width revision；
3. viewport 只拼接当前可见行及少量 overscan，不重建全部历史文本；
4. 新 chunk 只使活动 cell 和其后续 layout offset 失效；历史 immutable cell 不重算；
5. sticky-bottom 只更新 scroll offset；用户上翻后不因 chunk 强制回底，回到底部自动恢复；
6. scrollbar 的 max/value 来自同一份 visual-line index，不能与真实 viewport 分别估算；
7. resize 在 UI thread 生成新 layout generation，旧 generation 的测量/paint 结果丢弃；
8. prompt_toolkit renderer 继续做 cell diff，禁止常态 `renderer.clear()`；只在 terminal resume/不可恢复画布损坏时 full reset。

如果 `ScrollablePane` 无法在长 transcript、宽字符和持续更新下满足这些约束，应以自有 virtual transcript container 替换，而不是继续叠加 clear/repaint 补丁。

## 4. 时序要求

主 Agent transcript 必须保持：

```text
assistant pre-tool text
tool invocation
approval/review panel (if any)
tool live/result row
assistant post-tool text
```

工具开始是 assistant block 的硬边界。工具后的 provider chunk 必须创建新 AssistantCell，不能回写 pre-tool cell。

child 普通运行时序：

```text
root spawn_agent  -> register/schedule child -> return job ID
child event       -> ledger + execution panel
                  -> [approval only] root REVIEW
child result      -> typed parent mailbox + activity signal
root wait_agent   -> activity / human steering / timeout
parent safe point -> claim + inject + ack mailbox item
parent response   -> root transcript
```

主 Agent 或人类询问 child：

```text
root send_message / human /agents message
  -> typed directive(directive_id, delivery, generation, sequence)
  -> child 在下一模型安全边界消费
  -> report_to_parent(reply_to=directive_id)
  -> parent mailbox claim
  -> 父 Agent 在无 pending tool calls 的安全边界注入并 ack
```

所有 directive 都是异步投递；如果 root 确实需要回复，它在完成当前 provider tool-call/result 邻接后显式调用 `wait_agent`。reply correlation 仍由 `directive_id`/`reply_to` 提供，但 `wait_agent` 只报告 mailbox activity，不能伪装成只等待某个 RPC 的同步调用。旧 `awaited`/`detached` 约定废止。

### 4.1 `wait_agent` 时序

```text
root calls wait_agent
  -> atomically inspect pending mailbox + subscribe activity generation
  -> pending activity exists: return activity immediately
  -> otherwise race:
       child mailbox activity -> return activity
       human steering         -> return steered, then inject user input safely
       timeout                -> return timed_out
```

`wait_agent` 返回前必须先形成合法 tool result，随后才能把人类 steering 或 mailbox 内容注入 root context，保证 provider 的 assistant tool-call/tool-result adjacency。人类中断等待不会 cancel、pause 或重启任何 child。若 child 在 spawn 返回前或 wait 注册边界恰好完成，持久化 mailbox generation 必须让下一次 wait 立即观察到它，禁止 lost wakeup。

### 4.2 Guidance park 状态机

```text
queued -> running -> parking -> blocked -> resuming -> running
                                                `-> terminal
```

`request_guidance` 的原子顺序：

1. 分配 guidance request ID 并 ledger-first 记录请求；
2. 写入完整 tool result，保证 provider adjacency；
3. 原子检查 mailbox：若已有可用 directive，直接消费并继续；
4. 否则提交 replay checkpoint、剩余预算和 workspace/LSP generation；
5. 标记 `blocked`、发布 Attention，worker 确认 checkpoint 后正常退出并释放 process/slot；
6. 收到有效 resolution 后追加结构化 guidance tail，在新 worker process 中重放并重新调度同一 job。

blocked 期间暂停 active execution timeout，使用独立 guidance deadline；token/tool/round budget 不重置。session shutdown 后恢复为可见的 blocked job，不自动启动 worker。

### 4.3 必须收住的竞态和阻塞边界

- **spawn vs instant completion**：先注册 identity/mailbox，再启动 worker；child 瞬时完成也不能早于可寻址状态。
- **wait subscribe vs activity**：pending snapshot 与 activity generation/subscription 必须原子衔接，不能在检查为空与开始监听之间丢事件。
- **wait vs human steering**：两者并发到达时都持久化；本次 wait 可以优先返回 steered，但 mailbox activity 保留给下一安全边界，不能被吞掉。
- **timeout vs completion**：timeout 只结束 wait；截止点附近完成的 child 结果仍保留在 mailbox，下一次 wait/安全边界可见。
- **multiple child activity**：一次 wakeup 后批量 drain 当时已就绪的 mailbox items，按稳定 sequence 注入；不能靠重复短轮询制造上下文抖动。
- **parking vs directive**：消息在 parking 前后到达都不能丢；已有消息时不进入 blocked。
- **人类 vs 父 Agent回复**：未被 child 消费前，人类定向回复优先；消费后的后续消息作为普通 steering。
- **exactly-once**：parent mailbox 使用 queued→claimed→injected→acked；注入失败 release，crash 后未 ack 项重新可见。
- **protocol adjacency**：父或 child 有 pending tool calls 时都延迟外部消息注入。
- **compression race**：mailbox ledger-first；context rewrite 提交不能覆盖并发到达的 child/human 内容。
- **generation**：旧 session/generation 的 directive、reply 和 guidance resolution 拒绝注入。
- **cancel**：queued/running/approval/parking/blocked/resuming 均可取消；取消会关闭 guidance request 并阻止晚回复复活 job。
- **cancel vs active LLM request**：先持久化 cancellation epoch，再关闭 provider stream；关闭前到达的已记账 chunk 可审计，关闭后的 chunk 不得推进上下文或触发下一 round。
- **cancel vs effectful tool**：已完成的副作用不能假装回滚；尚未开始的调用禁止启动，运行中的 shell/peer 终止进程组，文件原语保持单次原子操作边界并记录最终事实。
- **cancel escalation**：cooperative cancel 超过 grace period 后升级为 worker hard kill；UI 必须区分 `cancelling`、`cancelled` 与 `killed`，不能提前显示成功停止。
- **worker death vs broker call**：worker 死亡先撤销 capability；broker 中尚未开始的调用丢弃，已开始的副作用按真实 outcome 入账，绝不能因调用方死亡假报回滚。
- **IPC reorder/duplication**：所有 child frame 携带 job、generation、epoch、sequence 和 idempotency key；supervisor 去重并拒绝旧 epoch，terminal 后不接受复活帧。
- **LSP ownership**：LSP server 与文档 generation 留在父进程；child 通过 broker 查询，blocked/resumed child 不持有可 stale 的 server 对象。
- **park vs worker exit**：只有 checkpoint 已持久化且 supervisor ack 后 worker 才退出；退出前收到 guidance 时可取消 park，退出后则必须由新 worker 从同一 transcript/预算恢复。
- **approval 隔离**：guidance 不是工具授权，父消息或 `/agents message` 不能自动批准 write/shell。
- **多 child**：Attention 可同时列出多个 blocked job，但回复必须携带 job/request ID，不能依赖当前 UI focus。
- **工作区变化**：blocked execute job 恢复时提示重新读取相关文件；保留独立 worktree，重新绑定 LSP generation。
- **父 turn 已结束**：消息持久化并提示 activity，不默认自主启动无限推理；下一个有效边界再注入。
- **registry fallback**：从 child schema 移除 `agent`/`update_plan` 后，ToolExecutor 也不能通过全局 registry fallback 绕过 scoped allowlist。

### 4.4 通信信任边界

child report 是证据和建议，不是授权或更高优先级指令。注入父上下文时必须保留 sender/job/kind/content hash，标注 delegated-worker data；child 无权借 report 修改 approval、mode 或全局 Plan。仓库中读取到的潜在 prompt injection 与 child 自己的结论应在结构上可区分。

## 5. 性能预算与验收

### 功能验收

- LSP completion 不出现 `None`，完整 LSP 结果仍进入 Agent tool result。
- bold、list、code、table 不显示原始 Markdown 控制符。
- child 的 assistant/tool/output/result 不进入主 transcript。
- child approval 仍可见、可取消、可 stale-refresh。
- child schema 中没有 `agent`、`update_plan`。
- child schema 中存在职责分离的 progress/report/guidance 三工具。
- effectful child tools 缺少 reason 时 fail closed；五类 child 查询工具不要求 reason 且不触发人工审批。
- 查询型 LSP 的 operation allowlist 被锁定；未来 effectful LSP 不会误继承免审批。
- Plan 只有 root writer，child progress 仍出现在 Agent panel。
- parent directive 与 child reply 可按 ID 关联；并发 child 不串线。
- spawn 始终立即返回 job ID，不因 child 运行时间阻塞；模型侧不再看到 `run_in_background`、`awaited`、`detached`。
- `wait_agent` 可由 child activity、人类 steering 或 timeout 结束；steering/timeout 均不取消 child。
- child 在 spawn/wait 边界瞬时完成不会 lost wakeup；多个完成按 mailbox sequence 稳定注入。
- root `list_agents` 与人类 Agent Panel 展示同源状态；root/human 均可强停 queued、running、approval、blocked child。
- 强停会中断真实 provider request、阻止下一轮和后续工具；超过 grace period 会硬杀 worker，late result 不进入父上下文。
- child worker 不直接实现 workspace/LSP/remote 工具；所有调用经父进程 scoped Tool Broker 复用同一路径和审批策略。
- worker crash、父进程退出和预算耗尽都会撤销 capability 并收敛为可观察 terminal，不留下孤儿生成请求。
- child 正常结束使用无 tool-call final response；prompt 的结论/证据/产物/未解决问题/置信度契约会被 runtime facts 校验补全。
- guidance park 不占 worker，恢复保持 tool adjacency、transcript 和剩余预算。
- blocked/cancel/session restore 的晚消息不会复活旧 generation job。

### 滚动与 stale 验收

- 1,000 个历史 cells 下连续滚轮/PageUp/PageDown 不出现残余边框、竖线或 scrollbar ghost。
- 中文、emoji、diff 背景、Markdown table 混排后反复 resize 不出现旧 generation 字符。
- 位于底部时流式输出 sticky-follow；离开底部后视口稳定；返回底部自动恢复。
- Ctrl+C、approval 切换、F2、session replay 后滚动状态仍一致。

### 性能验收

- 滚动一帧不重新 Markdown-parse 全部历史 cells。
- 静态历史 cell 在 width/theme 未变化时不重新生成 fragments。
- stream chunk 只更新活动 assistant/tool cell。
- 建立可重复 benchmark：100 / 500 / 1,000 cells，混合 CJK、emoji、Markdown、diff；记录 scroll frame time、resize rebuild time 与 chunk-to-paint latency。
- 目标：常规终端滚动保持稳定交互体感；具体毫秒阈值先由基线 benchmark 确定，再写入门禁，避免凭感觉设伪精确数字。

## 6. 原子提交建议

1. `fix(presentation): route child events away from root transcript`
2. `fix(tools): provide structured lsp completion summaries`
3. `fix(subagent): enforce non-recursive scoped capabilities`
4. `feat(subagent): require reasons for effectful child tools`
5. `feat(subagent): add typed report and guidance channels`
6. `feat(subagent): add interruptible mailbox activity waits`
7. `feat(subagent): add query and hard-stop lifecycle controls`
8. `refactor(subagent): isolate workers behind scoped tool ipc`
9. `feat(subagent): enforce structured final response contracts`
10. `feat(subagent): park and resume blocked child jobs`
11. `feat(cli): render retained assistant markdown`
12. `perf(cli): virtualize transcript layout and scrolling`
13. `test(cli): cover resize scroll stale and long transcript performance`

每批完成定向测试；最后运行 Ruff、Python 全量测试、Go peer 测试、build，并用真实 PTY 做 resize/scroll/stream/approval 验收。

## 7. 明确不做

- 不让 Go peer 解释 Markdown、subagent 或 tool reason。
- 不把 reason 加进 workspace/process primitive。
- 不因隐藏 child transcript 而丢弃 child runtime/history 数据。
- 不允许 child 写全局 Plan 或递归创建 Agent。
- 不在 spawn 内等待 child，不保留 `run_in_background`/`awaited`/`detached` 三套生命周期语义。
- 不把 `wait_agent` 做成忙轮询、worker join 或隐式取消。
- 不把“设置 cancelled 标志”冒充强制停止；未中断 provider transport 和工具进程就不算停止完成。
- 不尝试使用不安全的 Python thread kill；无法合作退出的 child 必须依赖可终止 worker process 隔离。
- 不用常态全屏 clear 作为 stale/性能修复。
- 除所有 child mode 统一增加 `list_file` 与查询型 `lsp` 外，不扩展其他 mode capability。
