# 审批交互与两阶段工具执行设计

## 1. 背景

为了支持未来的 VSCode 扩展、CLI 审批、TUI/Web 审批等多种交互形态，工具执行链路需要把“风险判定”和“审批交互”明确拆开。

当前系统已经具备以下基础能力：

- Hook 机制：负责在执行前后介入流程
- Policy 机制：负责对工具调用做规则判定
- UI Event 机制：负责把运行时事件传给界面层

但如果直接把 `y/n` 输入、VSCode 弹窗、Diff 预览等交互逻辑写进 policy 或 tool 本身，就会导致 domain 层与具体界面层强耦合，不利于后续扩展。

因此需要引入一套统一原则：

- policy 负责决定是否需要审批
- runtime 负责编排审批流程
- 各个 interface 层自行实现审批交互

## 2. 设计目标

### 2.1 目标

1. 支持高风险工具在执行前审批
2. 支持不同界面层实现不同审批方式
3. 支持编辑类工具在真正落盘前展示 diff
4. 支持命令类工具在真正执行前展示风险摘要
5. 保持 core runtime 不依赖 VSCode / CLI / TUI 的具体 UI API

### 2.2 非目标

1. 不在 policy 中直接调用终端输入或 VSCode API
2. 不让 tool 本身承担审批交互逻辑
3. 不在第一阶段就把所有工具都改为两阶段执行，只优先覆盖高风险工具

## 3. 核心原则

### 3.1 风险判定与审批交互分离

应明确分层：

- Policy：判断工具调用风险级别，决定 allow / deny / warn / require approval
- Runtime：接收策略决策，必要时发起审批请求
- Interface：真正向用户发起交互，并返回审批结果

这意味着：

- CLI 下可以用简单 `y/n/a` 输入审批
- VSCode 下可以用 modal、Quick Pick、diff editor 审批
- Web/TUI 下可以用自己的交互组件审批

但是这些交互差异都不应该进入 domain policy 层。

### 3.2 高风险工具优先采用两阶段执行

推荐首先拆分以下工具：

- `edit_file`
- `bash`

拆分目标不是为了让 tool 更复杂，而是为了支持“先预览，再提交”：

- prepare 阶段：生成预览结果，不产生最终副作用
- apply 阶段：在审批通过后，真正执行修改或命令

## 4. 推荐架构

## 4.1 三层职责

### A. Policy 层

负责纯规则判断，例如：

- 某命令是否明显危险，应该直接拒绝
- 某编辑是否属于源码/配置文件修改，应该要求审批
- 某操作是否只是低风险读取，可以直接放行

Policy 不负责：

- 终端提示输入
- VSCode 弹窗
- Diff UI 渲染

### B. Runtime / Executor 层

负责统一编排：

1. 收到 tool call
2. 运行 guard / policy
3. 如果需要审批，则生成 `ApprovalRequest`
4. 交给 `ApprovalProvider` 请求用户决策
5. 根据审批结果继续执行或拒绝执行
6. 产生结构化事件给界面层展示

### C. Interface 层

负责审批交互体验：

- CLI：打印摘要并等待 `y/n/a`
- VSCode：弹 warning、展示 diff editor、提供按钮
- TUI/Web：通过各自的界面组件实现审批

## 4.2 抽象建议

建议新增以下抽象对象：

### `ApprovalRequest`

用于描述一次需要用户决策的审批请求，建议包含：

- `id`
- `tool_name`
- `action_kind`
- `title`
- `message`
- `risk_level`
- `preview_text`
- `diff`
- `command`
- `cwd`
- `metadata`

### `ApprovalDecision`

用于描述界面层返回的审批结果，建议包含：

- `allow_once`
- `deny_once`
- `allow_session`
- `allow_tool_kind`

### `ApprovalProvider`

由各界面层自行实现，例如：

- `CLIApprovalProvider`
- `VSCodeApprovalProvider`
- `TUIApprovalProvider`

runtime 只依赖抽象接口，不依赖具体实现。

## 5. Guard / Policy 语义扩展建议

当前 guard 语义主要覆盖：

- allow
- deny
- warn

为了支持审批，建议扩展出第四种语义：

- require approval

实现上可以有两种方向：

### 方案 A：扩展 GuardDecision

在原有决策对象中增加字段，例如：

- `requires_approval: bool`
- `approval_payload: dict[str, Any]`

这样 executor 可以在看到 guard 结果后进入审批分支。

### 方案 B：新增审批型 hook

单独加入审批语义，但这会进一步增加 hook 模型复杂度。

当前更推荐方案 A，因为它更贴近现有 guard 机制，改造成本较低。

## 6. 两阶段工具设计

## 6.1 edit 工具

当前 `edit_file` 是：

1. 读取文件
2. 查找旧文本
3. 直接替换
4. 直接写回
5. 返回 diff

这个流程的问题是：diff 在副作用发生之后才出现，无法作为审批预览。

推荐改成：

### `edit_prepare`

输入：

- `file_path`
- `old_string`
- `new_string`

输出：

- `prepared_edit_id`
- `file_path`
- `before_hash`
- `after_text` 或临时产物引用
- `unified_diff`
- `summary`
- `risk_level`

特点：

- 不落盘
- 可供 CLI / VSCode 展示 diff
- 可作为审批依据

### `edit_apply`

输入：

- `prepared_edit_id`

执行：

- 校验源文件未发生意外变化
- 真正写盘
- 返回最终结果

这样可以避免“预览后文件已被外部修改”的问题。

## 6.2 bash 工具

当前 `bash` 是直接执行命令。

命令无法像文本编辑一样天然生成 diff，但仍然可以做两阶段拆分：

### `bash_prepare`

输入：

- `command`
- `timeout`

输出：

- `prepared_command_id`
- `command`
- `cwd`
- `risk_level`
- `risk_reasons`
- `estimated_effects`
- `requires_confirmation`

特点：

- 不真正执行命令
- 做静态分析和风险摘要
- 为 CLI / VSCode 提供审批内容

### `bash_apply`

输入：

- `prepared_command_id`

执行：

- 真正调用 shell
- 返回 stdout / stderr / exit code

## 7. CLI 审批交互建议

CLI 审批应由 interface 层单独实现，而不是塞进 policy 中。

推荐做一个 `CLIApprovalProvider`，交互形式保持简单：

### 对 edit_prepare

先展示：

- 文件路径
- 修改摘要
- diff 预览

再提供选项：

- `y`：允许本次
- `n`：拒绝本次
- `a`：本会话允许同类操作
- `v`：查看完整 diff

### 对 bash_prepare

先展示：

- 命令内容
- 当前目录
- 风险等级
- 风险原因
- 可能副作用

再提供同样的审批选项。

这样 CLI 依然只是一层界面实现，核心规则与运行时流程可复用到 VSCode。

## 8. VSCode 扩展交互建议

VSCode 侧应实现自己的 `ApprovalProvider`，不要把 VSCode API 引入 runtime。

推荐交互形式：

### 编辑类工具

- 打开原始文件与预览结果的 diff editor
- 在聊天面板或 webview 中展示修改摘要
- 允许用户点击 Apply / Reject

### bash 类工具

- 展示 command、cwd、风险标签、潜在副作用
- 用 modal 或 Quick Pick 进行审批

### 审计信息

建议保留一份执行轨迹：

- 发起的 tool call
- 是否经过审批
- 用户批准/拒绝结果
- 执行耗时
- 执行结果摘要

## 9. 推荐的执行时序

统一建议采用以下流程：

1. 模型发起工具调用
2. runtime 调用 policy / guard
3. policy 返回 allow / deny / warn / require approval
4. 若 require approval：
   - 生成 `ApprovalRequest`
   - 交给当前界面的 `ApprovalProvider`
   - 获取 `ApprovalDecision`
5. 若批准，进入 apply 阶段
6. 若拒绝，返回拒绝结果给 agent
7. 将审批事件和执行结果作为结构化事件广播给 UI 层

## 10. 推荐实施顺序

### Phase 1：先补审批抽象

- 扩展 guard 决策语义，支持 `require approval`
- 增加 `ApprovalRequest` / `ApprovalDecision` / `ApprovalProvider`
- 让 executor 能处理审批分支
- 先实现 CLI 的基础审批输入

### Phase 2：拆分 edit 为两阶段

- 引入 `edit_prepare`
- 引入 `edit_apply`
- CLI 先打印 unified diff
- 为后续 VSCode diff editor 做准备

### Phase 3：拆分 bash 为两阶段

- 引入 `bash_prepare`
- 引入 `bash_apply`
- 引入命令风险摘要与审批信息

### Phase 4：接入 VSCode 扩展

- 实现 `VSCodeApprovalProvider`
- 用原生 diff editor 做编辑审批
- 用 modal / Quick Pick 做命令审批

## 11. 职责边界总结

最终建议固定以下边界：

- Tool：负责 prepare/apply 或具体执行逻辑
- Policy：负责风险判定与审批触发条件
- Executor：负责调度 guard、approval、execute
- Interface：负责人与系统之间的审批交互
- Event/UI Bus：负责把审批和执行过程广播给具体界面

特别强调：

**审批触发条件可以由 policy 控制，但审批交互必须拆开，允许各个 interface 层自行实现。**

这条原则是后续支持 CLI、VSCode、TUI、Web 多端一致架构的关键。

## 12. Sub-agent 审批设计

Sub-agent 是审批体系里必须提前设计的一环，否则很容易出现“主 agent 受控，但子 agent 绕过审批”的问题。

### 12.1 基本原则

Sub-agent 不应拥有独立且脱离父级的审批体系，而应继承父级运行时的安全边界。

建议遵循以下规则：

1. 子 agent 默认继承父 agent 的 hook / policy 配置
2. 子 agent 默认复用父级的 `ApprovalProvider`
3. 子 agent 的审批事件必须带上父子链路元数据
4. 子 agent 不允许绕过父级 deny 规则
5. 子 agent 可以有更严格的局部限制，但不能比父级更宽松

### 12.2 继承与收敛策略

当前 [`HookRegistry.clone()`](reuleauxcoder/domain/hooks/registry.py:87) 已经提供了 hook 配置复制能力，这非常适合做 sub-agent 继承基础。

但仅复制 hook 不够，还应把以下信息继续向下传递：

- `session_id`
- `trace_id`
- `approval_provider`
- `approval_scope`
- `parent_agent_id`
- `agent_id`
- `delegation_depth`

这样每个审批请求都能回答三个问题：

- 是谁发起的
- 这是哪一层 agent 发起的
- 这次审批是否属于父级已授权范围

### 12.3 审批作用域建议

建议把审批授权做成带作用域的决策，而不是全局裸放行。

可参考的作用域：

- `once`：只允许当前这一次调用
- `tool_name`：允许当前会话内同名工具
- `agent_id`：允许当前 agent 后续同类调用
- `subtree`：允许某个父 agent 及其整个 sub-agent 子树
- `session`：允许整个当前会话

这里最关键的是不要把“允许一次”误扩散成“所有 sub-agent 都能直接通过”。

### 12.4 推荐默认策略

推荐默认采用保守策略：

1. 父 agent 对某次高风险操作的批准，默认只对当前 `agent_id` 生效
2. 子 agent 发起高风险操作时，仍然单独审批
3. 如果用户明确选择“允许该任务树后续同类操作”，才扩展到 `subtree`
4. 任意 `deny` 都应至少作用于当前请求，必要时可提升为当前子树 deny

换句话说：

- allow 不默认继承到所有 sub-agent
- deny 可以按需要向下继承
- 更安全的默认值是“逐层显式授权”

### 12.5 CLI / VSCode 的展示建议

无论是 CLI 还是 VSCode，sub-agent 审批都应该让用户看见 delegation 上下文，例如：

- `Main Agent -> Research Agent -> Edit Agent`
- 当前审批来自第几层 delegation
- 发起工具的 agent 名称 / id
- 该审批是否会影响整个子树

这样用户不会误以为所有审批都来自同一个顶层 agent。

### 12.6 事件与审计建议

审批事件建议补充以下字段：

- `agent_id`
- `parent_agent_id`
- `root_agent_id`
- `delegation_path`
- `delegation_depth`
- `approval_scope`

这样后续无论是 CLI 日志、VSCode activity view，还是审计回放，都可以清楚显示：

- 哪个 agent 发起了风险操作
- 用户批准的是单次、单 agent，还是整个子树
- 是否存在子 agent 连续请求高风险操作

### 12.7 实施建议

建议实现顺序如下：

1. 先让 sub-agent 复用父级 `ApprovalProvider`
2. 再为 `ApprovalRequest` 增加 agent 层级元数据
3. 最后再实现带作用域的授权缓存，例如按 `agent_id + tool_name + scope` 存储

这样可以先保证不会绕过审批，再逐步增强用户体验。

## 13. Sub-agent Profile 预设权限模型

除了逐次审批之外，sub-agent 还应支持预定义 profile，用来在创建时就收紧能力边界。

这样做的价值在于：

- 减少每次 delegation 都临时拼权限规则
- 让模型明确知道当前 sub-agent 的职责范围
- 让审批系统能基于 profile 做默认策略
- 降低“探索型子代理误触发写入/执行操作”的风险

### 13.1 基本原则

profile 应作为 sub-agent 的静态权限边界，优先级高于运行时审批放行。

也就是说：

- profile 禁止的能力，不能靠一次审批临时突破
- 审批只能在 profile 允许的能力范围内决定是否执行
- profile 决定“最多能做什么”
- approval 决定“这一次要不要做”

这能保证系统同时具备：

- 固定边界
- 灵活审批

### 13.2 推荐内置 profile

#### `explorer`

只允许探索，不允许产生副作用。

适合场景：

- 阅读代码
- 搜索文件
- 收集上下文
- 生成分析结论

建议允许：

- [`read_file`](reuleauxcoder/extensions/tools/builtin/read.py:8)
- [`glob`](reuleauxcoder/extensions/tools/builtin/glob.py:8)
- [`grep`](reuleauxcoder/extensions/tools/builtin/grep.py:20)

建议禁止：

- [`write_file`](reuleauxcoder/extensions/tools/builtin/write.py:8)
- [`edit_file`](reuleauxcoder/extensions/tools/builtin/edit.py:9)
- [`bash`](reuleauxcoder/extensions/tools/builtin/bash.py:10)
- 高风险或未知副作用的 MCP 工具
- 再次创建高权限 sub-agent

这是最适合默认研究型 sub-agent 的 profile。

#### `editor`

允许文件修改，但不允许 shell 执行和未知远程副作用。

适合场景：

- 在已知 workspace 内做代码修改
- 生成 patch
- 执行受限写入

建议允许：

- [`read_file`](reuleauxcoder/extensions/tools/builtin/read.py:8)
- [`glob`](reuleauxcoder/extensions/tools/builtin/glob.py:8)
- [`grep`](reuleauxcoder/extensions/tools/builtin/grep.py:20)
- [`edit_file`](reuleauxcoder/extensions/tools/builtin/edit.py:9)
- [`write_file`](reuleauxcoder/extensions/tools/builtin/write.py:8)

建议禁止或强限制：

- [`bash`](reuleauxcoder/extensions/tools/builtin/bash.py:10)
- 未分类的 MCP 副作用工具
- 生产环境或网络侧 effect 工具

#### `operator`

允许执行命令和修改文件，但应始终挂在更严格审批下。

适合场景：

- 跑测试
- 安装依赖
- 执行构建
- 进行受控修改

建议允许：

- 上述读写工具
- [`bash`](reuleauxcoder/extensions/tools/builtin/bash.py:10)
- 已知安全类别的 MCP 工具

建议仍需审批：

- 任意 shell 执行
- 敏感文件改动
- 未知 MCP 工具

#### `custom`

允许上层明确指定允许/禁止的工具集合，用于特殊任务。

适合场景：

- IDE 扩展按按钮创建专用 agent
- 某些 MCP server 的定制代理
- 实验性能力组合

### 13.3 Profile 与审批的关系

建议按两道门理解：

1. **Profile Gate**：判断当前 agent 是否有资格调用某类工具
2. **Approval Gate**：在有资格的前提下，决定这一次是否允许执行

例如：

- `explorer` 调用 [`bash`](reuleauxcoder/extensions/tools/builtin/bash.py:10) → 直接拒绝，不进入审批
- `editor` 调用 [`edit_file`](reuleauxcoder/extensions/tools/builtin/edit.py:9) → 允许进入审批或 prepare/apply
- `operator` 调用高风险 MCP 工具 → profile 允许，但仍需审批

所以 profile 是 capability boundary，approval 是 runtime decision。

### 13.4 对 MCP 工具的 profile 策略

由于外部 MCP 工具不可控，profile 对 MCP 应默认更保守。

建议做以下分类：

- `mcp.read_only`
- `mcp.workspace_write`
- `mcp.process_execute`
- `mcp.remote_side_effect`
- `mcp.unknown`

如果无法可靠分类，则归为 `mcp.unknown`。

推荐默认：

- `explorer`：只允许 `mcp.read_only`
- `editor`：允许 `mcp.read_only`，谨慎允许 `mcp.workspace_write`
- `operator`：允许 `mcp.read_only` / `mcp.workspace_write` / `mcp.process_execute`，但高风险调用仍需审批
- `mcp.unknown`：所有 profile 默认 require approval，必要时直接 deny

### 13.5 Profile 的实现建议

实现时不要只存 profile 名称，而应展开成结构化能力集，例如：

- `allowed_tool_names`
- `denied_tool_names`
- `allowed_effect_classes`
- `max_delegation_depth`
- `allow_spawn_subagent`
- `mcp_policy_mode`

这样：

- CLI 可以显示当前 sub-agent profile
- VSCode 可以展示 profile badge
- policy 可以基于 profile 快速判断是否直接 deny

### 13.6 与 Sub-agent 继承的关系

父 agent 创建子 agent 时，建议显式指定 profile，而不是默认继承完整能力。

推荐规则：

1. 子 agent profile 默认不高于父 agent
2. `explorer` 可以创建另一个 `explorer`
3. `explorer` 不能直接创建 `operator`
4. `editor` 若创建 `explorer`，属于降权 delegation，可直接允许
5. 提升 profile 等级应需要额外审批或直接禁止

这能避免 delegation 过程里的隐式提权。

### 13.7 推荐默认策略

如果没有显式指定 profile，sub-agent 默认使用：

- 研究/检索型任务 → `explorer`
- 修改型任务 → `editor`
- 执行型任务 → `operator`，且必须更强审批

而从系统安全角度看，更推荐：

- 默认一律 `explorer`
- 只有当上层明确申请更高能力时，才切到 `editor` / `operator`

这样更符合最小权限原则。

## 14. 审批接口实现路径

这一节聚焦“接口怎么落地”，也就是从当前代码出发，如何把审批机制真正接进 runtime、policy、界面层，而不是只停留在概念设计。

### 14.1 总体分层

建议审批接口沿着四层展开：

1. **Domain Types**：定义审批请求、审批结果、审批作用域等结构
2. **Policy / Guard**：判断是否需要审批，并生成审批请求载荷
3. **Runtime / Executor**：接入审批 provider，阻塞等待结果，再决定是否继续执行
4. **Interface Adapters**：CLI / VSCode / TUI 各自实现自己的审批交互

对应当前代码结构，最合适的落点分别是：

- Hook / guard 类型：[`reuleauxcoder/domain/hooks/types.py`](reuleauxcoder/domain/hooks/types.py)
- Hook 抽象：[`reuleauxcoder/domain/hooks/base.py`](reuleauxcoder/domain/hooks/base.py:13)
- Tool 执行编排：[`ToolExecutor.execute()`](reuleauxcoder/domain/agent/tool_execution.py:25)
- CLI 交互入口：[`run_repl()`](reuleauxcoder/interfaces/cli/repl.py:17) 与 [`main()`](reuleauxcoder/interfaces/cli/main.py:28)
- UI 事件桥：[`UIEventBus`](reuleauxcoder/interfaces/events.py:95)

### 14.2 第一层：补齐审批领域模型

建议先新增一组独立的审批模型，例如放在新文件：

- [`reuleauxcoder/domain/approval/models.py`](reuleauxcoder/domain/approval/models.py)
- [`reuleauxcoder/domain/approval/protocols.py`](reuleauxcoder/domain/approval/protocols.py)

建议最少包含：

#### `ApprovalRequest`

字段建议：

- `id`
- `tool_name`
- `tool_source`
- `arguments`
- `title`
- `message`
- `risk_level`
- `effect_class`
- `preview_text`
- `diff_text`
- `metadata`
- `agent_id`
- `parent_agent_id`
- `root_agent_id`
- `profile`
- `delegation_depth`

#### `ApprovalDecision`

字段建议：

- `approved: bool`
- `scope`
- `reason`
- `remember: bool`
- `expires_at`

#### `ApprovalScope`

建议枚举：

- `once`
- `tool_name`
- `agent_id`
- `subtree`
- `session`

#### `ApprovalProvider`

建议协议接口：

- `request_approval(request: ApprovalRequest) -> ApprovalDecision`

这里保持同步接口最简单，因为当前 [`ToolExecutor.execute()`](reuleauxcoder/domain/agent/tool_execution.py:25) 本身就是同步编排。

### 14.3 第二层：扩展 guard 决策语义

当前 [`GuardDecision`](reuleauxcoder/domain/hooks/types.py:76) 只有 allow / deny / warn 三类语义承载方式，不足以完整表达“需要审批”。

建议做两步：

#### 步骤 A：扩展 [`GuardDecision`](reuleauxcoder/domain/hooks/types.py:76)

增加类似字段：

- `requires_approval: bool = False`
- `approval_request: ApprovalRequest | None = None`

这样已有 [`GuardHook`](reuleauxcoder/domain/hooks/base.py:22) 机制不用推翻，仍然是 guard 决定控制流。

#### 步骤 B：升级 policy 输入信息

当前 [`ToolPolicy.evaluate()`](reuleauxcoder/extensions/tools/policies/base.py:14) 只收 [`ToolCall`](reuleauxcoder/domain/llm/models.py:9)，这对于 MCP 不够。

建议后续改成接收一个更完整的上下文，例如：

- `tool_call`
- `tool_name`
- `tool_description`
- `tool_schema`
- `tool_source`
- `agent/profile metadata`

这样 policy 才能同时覆盖：

- 内置 [`write_file`](reuleauxcoder/extensions/tools/builtin/write.py:8)
- 内置 [`bash`](reuleauxcoder/extensions/tools/builtin/bash.py:10)
- 外部 [`MCPTool.execute()`](reuleauxcoder/extensions/mcp/adapter.py:24) 所包装的任意工具

### 14.4 第三层：在 executor 中接审批分支

真正的实现关键在 [`ToolExecutor.execute()`](reuleauxcoder/domain/agent/tool_execution.py:25)。

当前流程是：

1. 构造 `BeforeToolExecuteContext`
2. 跑 guards
3. 有 deny 就返回
4. 跑 transform / observer
5. 真正执行工具

建议改成：

1. 构造 `BeforeToolExecuteContext`
2. 跑 guards
3. 若 deny → 直接返回
4. 若 `requires_approval` → 调用 `ApprovalProvider`
5. 若用户拒绝 → 返回拒绝结果
6. 若用户批准 → 写入授权缓存或 metadata
7. 再进入 transform / observer / 真正执行工具

也就是说，审批应该位于：

- guard 之后
- tool 执行之前

这是最自然的位置，因为：

- policy 已经做完风险判断
- tool 还没有产生副作用
- transform / observer 还能看到审批后的上下文

### 14.5 第四层：ApprovalProvider 注入路径

建议不要把 provider 放进 policy 或 tool，而是放进 runtime/app context。

推荐注入链路：

1. 在 [`AppContext`](reuleauxcoder/interfaces/entrypoint/runner.py:29) 中增加 `approval_provider`
2. 在 [`AppRunner.initialize()`](reuleauxcoder/interfaces/entrypoint/runner.py:92) 中根据当前界面创建 provider
3. 将 provider 注入到 `Agent` 或 `ToolExecutor`
4. [`ToolExecutor.execute()`](reuleauxcoder/domain/agent/tool_execution.py:25) 统一调用

这条路径的好处是：

- CLI 模式和 VSCode 模式只是在入口处换不同 provider
- domain 层不依赖 prompt_toolkit 或 VSCode API
- sub-agent 可以显式继承父级 provider

### 14.6 第五层：CLI 审批接口实现

CLI 端建议先做最小可用版。

建议新增：

- [`reuleauxcoder/interfaces/cli/approval.py`](reuleauxcoder/interfaces/cli/approval.py)

实现一个：

- `CLIApprovalProvider`

建议职责：

1. 接收 `ApprovalRequest`
2. 打印 tool 名、参数摘要、风险等级、profile、delegation 信息
3. 如果有 diff/preview，先渲染摘要
4. 读取用户输入：`y / n / a / v`
5. 返回 `ApprovalDecision`

其中：

- `y`：允许本次
- `n`：拒绝本次
- `a`：当前会话记住
- `v`：查看完整 diff / 参数详情后重新询问

CLI 的显示仍然建议通过 [`UIEventBus`](reuleauxcoder/interfaces/events.py:95) 走统一事件链，这样后续 TUI/日志系统都能复用。

### 14.7 第六层：VSCode 审批接口实现

VSCode 端建议也实现同名语义但不同交互的 provider，例如：

- [`VSCodeApprovalProvider`](docs/tool-approval-design.md)

其职责与 CLI 相同，但 UI 形式不同：

- 编辑类工具：弹出 diff editor + Apply / Reject
- 命令类工具：modal / quick pick
- MCP 工具：展示参数 JSON、schema 摘要、风险标签

接口层不需要知道具体 tool 实现源码，只需要消费 `ApprovalRequest` 中已经结构化好的信息。

### 14.8 第七层：授权缓存接口

审批接口只做“问用户一次”还不够，必须同时设计授权缓存。

建议新增：

- [`reuleauxcoder/domain/approval/store.py`](reuleauxcoder/domain/approval/store.py)

建议提供：

- `lookup(request) -> ApprovalDecision | None`
- `remember(request, decision) -> None`
- `clear(scope) -> None`

缓存 key 建议至少包含：

- `tool_name`
- `scope`
- `agent_id`
- `profile`
- `session_id`
- 可选的 effect class / MCP classification

这样：

- `allow once` 不会泄漏成全局放行
- `allow session` 可复用于当前会话
- `allow subtree` 可服务 sub-agent 链路

### 14.9 第八层：与两阶段工具对接

审批接口不应只服务直接执行型工具，也应服务 prepare/apply 工具。

建议规则：

- `edit_prepare` / `bash_prepare` 阶段构造 `ApprovalRequest`
- UI 层消费 preview/diff
- `edit_apply` / `bash_apply` 阶段只接受带授权上下文的请求

这样审批接口与两阶段工具是解耦但协作的：

- preview 由 tool prepare 负责
- 批准/拒绝由 approval provider 负责
- 最终提交由 apply 负责

### 14.10 第九层：与 MCP 工具对接

MCP 是审批接口设计里必须优先考虑的场景，因为你无法控制外部实现源码。

当前 MCP 工具通过 [`MCPTool`](reuleauxcoder/extensions/mcp/adapter.py:11) 适配成统一 [`Tool`](reuleauxcoder/extensions/tools/base.py:6) 接口，并暴露：

- `name`
- `description`
- `parameters`

这意味着审批接口完全可以在执行前提取：

- tool source = `mcp`
- tool name
- description
- input schema
- 当前 arguments

然后交给 policy / approval pipeline 做判断。

建议把 MCP 工具默认纳入更保守策略：

- 若 effect class 未知，则默认 `requires_approval`
- 若参数包含明显危险字段（command/path/sql/url/body 等），提高风险等级
- 若命中 deny 规则，直接拒绝

### 14.11 第十层：落地顺序

建议按这个顺序实现：

#### Phase 1：领域抽象

- 新增 `ApprovalRequest` / `ApprovalDecision` / `ApprovalScope`
- 给 [`GuardDecision`](reuleauxcoder/domain/hooks/types.py:76) 加 `requires_approval`

#### Phase 2：runtime 接线

- 给 [`ToolExecutor.execute()`](reuleauxcoder/domain/agent/tool_execution.py:25) 增加审批分支
- 引入 provider 与授权缓存

#### Phase 3：CLI MVP

- 新增 `CLIApprovalProvider`
- 支持 `y/n/a/v`
- 支持显示参数摘要与 diff 摘要

#### Phase 4：policy 升级

- 让 policy 能看到 tool metadata / MCP metadata / sub-agent metadata
- 增加 effect class 与 unknown tool 策略

#### Phase 5：VSCode 接入

- 实现 `VSCodeApprovalProvider`
- 对编辑类工具接 diff UI
- 对命令类 / MCP 工具接 modal 和详情展示

### 14.12 一句话总结

审批接口的实现路径，核心不是“在哪弹一个确认框”，而是把下面几件事串成一条统一链路：

**policy 判定 → guard 表达 require approval → executor 调 provider → provider 走界面交互 → decision 回流 executor → 再决定是否真正执行工具。**

只要这条链路打通，CLI、VSCode、TUI、MCP、sub-agent 都能挂在同一套审批体系上。
