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
  agent, update_plan, report_progress, normal mode tools

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

`report_to_parent` 使用 typed mailbox，至少携带 `message_id`、`reply_to`、`kind`、sender/recipient、job、generation、sequence、content hash。主 Agent 通过 root-only `agent(action=message)` 询问 child；人类通过 `/agents message` 走同一个 directive channel。child 收到 directive 后可用 `report_to_parent(kind=reply, reply_to=directive_id)` 精确回复。

`request_guidance` 不在 worker thread 内长期 wait，而是 checkpoint-and-park：完成当前 tool call/result 邻接、持久化 transcript/checkpoint、把 job 标为 `blocked`、释放并发槽；收到父 Agent 或人类的定向 guidance 后，再从同一 job/transcript 恢复。

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
10. 恢复旧 session 时遇到 child 发起 `agent`/`update_plan`，按 unavailable tool fail closed，不补发能力。

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
child event -> ledger + execution panel
            -> [approval only] root REVIEW
child result -> immediate parent mailbox/context
parent response -> root transcript
```

主 Agent 或人类询问 child：

```text
root agent(action=message) / human /agents message
  -> typed directive(directive_id, delivery, generation, sequence)
  -> child 在下一模型安全边界消费
  -> report_to_parent(reply_to=directive_id)
  -> parent mailbox claim
  -> 父 Agent 在无 pending tool calls 的安全边界注入并 ack
```

主 Agent 发起的语义询问默认可标记为 `awaited`，父 loop 等到对应 reply、child blocker/terminal、用户 steering 或超时；普通通知使用 `detached`，不强制父 Agent等待。

### 4.1 Guidance park 状态机

```text
queued -> running -> parking -> blocked -> resuming -> running
                                                `-> terminal
```

`request_guidance` 的原子顺序：

1. 分配 guidance request ID 并 ledger-first 记录请求；
2. 写入完整 tool result，保证 provider adjacency；
3. 原子检查 mailbox：若已有可用 directive，直接消费并继续；
4. 否则提交 replay checkpoint、剩余预算和 workspace/LSP generation；
5. 标记 `blocked`、发布 Attention、释放 worker slot；
6. 收到有效 resolution 后追加结构化 guidance tail，重新调度同一 job。

blocked 期间暂停 active execution timeout，使用独立 guidance deadline；token/tool/round budget 不重置。session shutdown 后恢复为可见的 blocked job，不自动启动 worker。

### 4.2 必须收住的竞态和阻塞边界

- **parking vs directive**：消息在 parking 前后到达都不能丢；已有消息时不进入 blocked。
- **人类 vs 父 Agent回复**：未被 child 消费前，人类定向回复优先；消费后的后续消息作为普通 steering。
- **exactly-once**：parent mailbox 使用 queued→claimed→injected→acked；注入失败 release，crash 后未 ack 项重新可见。
- **protocol adjacency**：父或 child 有 pending tool calls 时都延迟外部消息注入。
- **compression race**：mailbox ledger-first；context rewrite 提交不能覆盖并发到达的 child/human 内容。
- **generation**：旧 session/generation 的 directive、reply 和 guidance resolution 拒绝注入。
- **cancel**：queued/running/approval/parking/blocked/resuming 均可取消；取消会关闭 guidance request 并阻止晚回复复活 job。
- **approval 隔离**：guidance 不是工具授权，父消息或 `/agents message` 不能自动批准 write/shell。
- **多 child**：Attention 可同时列出多个 blocked job，但回复必须携带 job/request ID，不能依赖当前 UI focus。
- **工作区变化**：blocked execute job 恢复时提示重新读取相关文件；保留独立 worktree，重新绑定 LSP generation。
- **父 turn 已结束**：消息持久化并提示 activity，不默认自主启动无限推理；下一个有效边界再注入。
- **registry fallback**：从 child schema 移除 `agent`/`update_plan` 后，ToolExecutor 也不能通过全局 registry fallback 绕过 scoped allowlist。

### 4.3 通信信任边界

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
6. `feat(subagent): park and resume blocked child jobs`
7. `feat(cli): render retained assistant markdown`
8. `perf(cli): virtualize transcript layout and scrolling`
9. `test(cli): cover resize scroll stale and long transcript performance`

每批完成定向测试；最后运行 Ruff、Python 全量测试、Go peer 测试、build，并用真实 PTY 做 resize/scroll/stream/approval 验收。

## 7. 明确不做

- 不让 Go peer 解释 Markdown、subagent 或 tool reason。
- 不把 reason 加进 workspace/process primitive。
- 不因隐藏 child transcript 而丢弃 child runtime/history 数据。
- 不允许 child 写全局 Plan 或递归创建 Agent。
- 不用常态全屏 clear 作为 stale/性能修复。
- 除所有 child mode 统一增加 `list_file` 与查询型 `lsp` 外，不扩展其他 mode capability。
