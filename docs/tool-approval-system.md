# Tool Approval System

本文档描述 ReuleauxCoder 的工具权限管理与审批机制。

## 1. 架构概览

工具权限系统采用分层设计：

```
┌─────────────────────────────────────────────────────────────┐
│                    Interface Layer                           │
│  CLIApprovalProvider / (Future: VSCodeApprovalProvider)     │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Runtime Layer                             │
│  ToolExecutor → HookRegistry.run_guards()                   │
│               → ApprovalProvider.request_approval()          │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Policy Layer                              │
│  ToolPolicyGuardHook                                         │
│    ├── Built-in Policies (BashDangerousCommandPolicy)       │
│    └── ApprovalPolicyEngine (config-driven)                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Config Layer                              │
│  ApprovalConfig / ApprovalRuleConfig                         │
│  ConfigLoader → config.yaml                                  │
└─────────────────────────────────────────────────────────────┘
```

**核心组件：**

| 组件 | 位置 | 职责 |
|------|------|------|
| [`ToolPolicyGuardHook`](reuleauxcoder/domain/hooks/builtin/tool_policy.py:15) | Policy | 工具执行前的统一拦截点，协调内置策略与配置策略 |
| [`ApprovalPolicyEngine`](reuleauxcoder/domain/approval_engine.py:36) | Policy | 基于配置规则的审批决策引擎 |
| [`ToolExecutor`](reuleauxcoder/domain/agent/tool_execution.py:20) | Runtime | 执行工具调用，处理 guard 决策与审批流程 |
| [`ApprovalProvider`](reuleauxcoder/domain/approval.py:44) | Interface | 审批交互接口，由各界面层实现 |
| [`ApprovalConfig`](reuleauxcoder/domain/config/models.py:54) | Config | 审批配置模型 |

## 2. 执行流程

### 2.1 工具调用拦截

当 Agent 需要执行工具时，流程如下：

```
Agent.chat() → AgentLoop → ToolExecutor.execute()
                              │
                              ▼
                    构建 BeforeToolExecuteContext
                    (包含 tool_source, mcp_server 等元数据)
                              │
                              ▼
                    HookRegistry.run_guards()
                              │
                              ▼
                    ToolPolicyGuardHook.run()
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
            Built-in Policies    ApprovalPolicyEngine
                    │                   │
                    └─────────┬─────────┘
                              ▼
                        GuardDecision
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
        deny            require_approval      allow/warn
            │                 │                 │
            ▼                 ▼                 ▼
      返回错误信息      调用 ApprovalProvider   继续执行工具
```

### 2.2 GuardDecision 语义

[`GuardDecision`](reuleauxcoder/domain/hooks/types.py:76) 定义了四种决策结果：

| 决策 | 含义 | 后续行为 |
|------|------|----------|
| `allow()` | 允许执行 | 继续执行工具 |
| `warn(message)` | 警告但允许 | 输出警告，继续执行 |
| `require_approval(reason)` | 需要审批 | 调用 ApprovalProvider |
| `deny(reason)` | 拒绝执行 | 返回错误信息，不执行工具 |

### 2.3 审批交互

当 guard 返回 `require_approval` 时：

```python
# ToolExecutor.execute() 中的处理逻辑
approval_required = next((d for d in guard_decisions if d.requires_approval), None)
if approval_required is not None:
    provider = self.agent.approval_provider
    if provider is None:
        return "Tool requires approval, but no approval provider is configured"
    decision = provider.request_approval(ApprovalRequest(...))
    if not decision.approved:
        return decision.reason or "Tool denied by approval provider"
```

## 3. 策略层详解

### 3.1 内置安全策略

内置策略提供不可绕过的安全底线，当前实现：

**[`BashDangerousCommandPolicy`](reuleauxcoder/extensions/tools/policies/bash.py:14)**

拦截明显危险的 shell 命令：

| 模式 | 说明 |
|------|------|
| `rm -rf /` | 递归删除根目录 |
| `rm -rf ~` | 递归删除用户目录 |
| `mkfs` | 格式化文件系统 |
| `dd ... of=/dev/` | 原始磁盘写入 |
| `curl \| bash` | 管道执行远程脚本 |
| `wget \| bash` | 管道执行远程脚本 |
| fork bomb | fork 炸弹 |

内置策略直接返回 `deny`，不进入审批流程。

### 3.2 配置驱动策略

[`ApprovalPolicyEngine`](reuleauxcoder/domain/approval_engine.py:36) 根据配置规则决定工具的审批策略。

**匹配维度：**

| 字段 | 说明 | 示例 |
|------|------|------|
| `tool_name` | 工具名称 | `write_file`, `bash` |
| `tool_source` | 工具来源 | `builtin`, `mcp` |
| `mcp_server` | MCP 服务器名 | `filesystem`, `postgres` |
| `effect_class` | 效果分类 (预留) | `read`, `write`, `execute` |
| `profile` | 子代理配置 (预留) | `explorer`, `editor` |

**优先级规则：**

规则按特异性排序，更具体的规则优先匹配：

```
tool_name (+4) > mcp_server (+2) > tool_source (+1) > effect_class (+1) > profile (+1)
```

示例：`tool_source=mcp, mcp_server=filesystem, tool_name=write_file` 的规则会覆盖 `tool_source=mcp, mcp_server=filesystem` 的规则。

**动作类型：**

| 动作 | 行为 |
|------|------|
| `allow` | 直接允许执行 |
| `warn` | 输出警告后允许执行 |
| `require_approval` | 要求人工审批 |
| `deny` | 直接拒绝执行 |

## 4. 配置模型

### 4.1 配置结构

```yaml
approval:
  default_mode: require_approval  # 默认动作
  rules:
    # 内置工具规则
    - tool_name: read_file
      action: allow
    - tool_name: write_file
      action: require_approval
    
    # MCP 工具规则
    - tool_source: mcp
      action: require_approval
    - tool_source: mcp
      mcp_server: filesystem
      action: warn
    - tool_source: mcp
      mcp_server: filesystem
      tool_name: write_file
      action: require_approval
```

### 4.2 默认规则

系统内置以下默认规则（定义于 [`domain/config/schema.py`](reuleauxcoder/domain/config/schema.py:52)）：

| 工具 | 动作 |
|------|------|
| `read_file`, `glob`, `grep` | `allow` |
| `write_file`, `edit_file`, `bash`, `agent` | `require_approval` |
| MCP 工具 (通用) | `require_approval` |
| MCP `filesystem` server | `warn` |

默认 `default_mode` 为 `require_approval`。

### 4.3 配置加载

配置按以下优先级加载（后者覆盖前者）：

1. 全局配置：`~/.rcoder/config.yaml`
2. 工作区配置：`./.rcoder/config.yaml`
3. 命令行指定：`--config path/to/config.yaml`

## 5. MCP 工具集成

MCP 工具通过元数据接入审批系统：

```python
# MCPTool 设置元数据
class MCPTool(Tool):
    tool_source = "mcp"  # 标识为 MCP 工具
    self.server_name = tool_info.server_name  # 记录来源服务器
```

执行时，[`ToolExecutor`](reuleauxcoder/domain/agent/tool_execution.py:26) 将这些元数据注入 `BeforeToolExecuteContext.metadata`，供策略引擎使用。

**支持的配置粒度：**

1. 所有 MCP 工具：`tool_source: mcp`
2. 特定服务器：`tool_source: mcp, mcp_server: xxx`
3. 特定工具：`tool_source: mcp, mcp_server: xxx, tool_name: yyy`

## 6. CLI 审批实现

### 6.1 CLIApprovalProvider

[`CLIApprovalProvider`](reuleauxcoder/interfaces/cli/approval.py:11) 实现命令行交互式审批：

```
Approval required for tool 'write_file' (builtin)
Reason: Tool 'write_file' requires approval by policy
Args: {'file_path': '/path/to/file', 'content': '...'}
Approve tool execution? [y/n]: 
```

### 6.2 注入方式

在 CLI 启动时注入：

```python
# interfaces/cli/main.py
ctx = runner.initialize()
ctx.agent.approval_provider = CLIApprovalProvider(ctx.ui_bus)
```

## 7. 运行时管理

### 7.1 查看规则

```
/approval          # 显示当前规则
/approval show     # 同上
```

输出示例：

```
Approval default_mode: require_approval
Configured rules:
  1. tool=read_file -> allow
  2. tool=write_file -> require_approval
  3. source=mcp, mcp_server=filesystem -> warn
MCP effective policy view:
  MCP server filesystem -> warn
    [dim]configured at server level[/dim]
    - read_file -> warn
      [dim]inherited from server filesystem[/dim]
```

### 7.2 修改规则

```
/approval set <target> <action>
```

**支持的 target 格式：**

| 格式 | 说明 | 示例 |
|------|------|------|
| `tool:<name>` | 内置工具 | `tool:bash` |
| `mcp` | 所有 MCP 工具 | `mcp` |
| `mcp:<server>` | 特定 MCP 服务器 | `mcp:filesystem` |
| `mcp:<server>:<tool>` | 特定 MCP 工具 | `mcp:filesystem:write_file` |

**示例：**

```
/approval set tool:bash allow
/approval set mcp:filesystem warn
/approval set mcp:postgres:query deny
```

### 7.3 持久化与热更新

规则修改后：

1. 写入工作区配置 `.rcoder/config.yaml`
2. 调用 [`ToolPolicyGuardHook.update_approval_config()`](reuleauxcoder/domain/hooks/builtin/tool_policy.py:34) 热更新运行时策略

无需重启即可生效。

## 8. 扩展点

### 8.1 添加新的内置策略

```python
# extensions/tools/policies/xxx.py
class XxxPolicy(ToolPolicy):
    def evaluate(self, tool_call: ToolCall) -> GuardDecision | None:
        if tool_call.name != "xxx":
            return None
        # 实现策略逻辑
        return GuardDecision.allow()

# extensions/tools/policies/__init__.py
DEFAULT_TOOL_POLICIES = (
    BashDangerousCommandPolicy(),
    XxxPolicy(),  # 添加新策略
)
```

### 8.2 实现新的审批界面

```python
# interfaces/vscode/approval.py
class VSCodeApprovalProvider(ApprovalProvider):
    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        # 实现VSCode扩展的审批交互
        # 例如：弹出对话框、显示diff等
        pass
```

### 8.3 添加新的匹配维度

1. 扩展 [`ApprovalRuleConfig`](reuleauxcoder/domain/config/models.py:42) 添加新字段
2. 扩展 [`ToolApprovalContext`](reuleauxcoder/domain/approval_engine.py:15) 添加对应字段
3. 在 [`ToolPolicyGuardHook.run()`](reuleauxcoder/domain/hooks/builtin/tool_policy.py:38) 中填充新字段
4. 更新 [`_matches()`](reuleauxcoder/domain/approval_engine.py:71) 方法支持新维度

## 9. 当前限制

| 限制 | 说明 |
|------|------|
| 单次审批 | 仅支持 `allow_once`/`deny_once`，无记忆功能 |
| 同步阻塞 | 审批过程阻塞工具执行，不支持异步 |
| CLI 优先 | 目前仅 CLI 实现了 ApprovalProvider |
| 参数级分析 | 未实现基于参数内容的风险分析 |
| 审计日志 | 未实现审批决策的持久化审计 |
| 二阶段执行 | `edit`/`bash` 的 diff 预览尚未实现 |
