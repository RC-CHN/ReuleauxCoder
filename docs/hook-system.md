# Hook 系统设计文档

## 1. 概述

ReuleauxCoder 的 Hook 系统是一个 AOP（面向切面编程）机制，用于在关键执行点介入并影响执行流程。它与现有事件体系互补：

- **Event**：表示系统已经发生的事情，通知外部观察者，驱动 UI / 日志 / telemetry
- **Hook**：介入执行点，读取上下文，修改 payload，阻止继续执行

> Event 负责"发生了什么"，Hook 负责"在发生之前或之后如何介入"。

---

## 2. 核心抽象

### 2.1 HookKind

Hook 分为三种语义类别：

| Kind | 作用 | 返回值 | 异常语义 |
|------|------|--------|----------|
| `GUARD` | 决定是否允许继续执行 | `GuardDecision` | fail-closed（异常即拒绝） |
| `TRANSFORM` | 修改上下文数据 | 同类型 Context | 异常中断执行 |
| `OBSERVER` | 观察执行，不修改控制流 | `None` | fail-open（异常继续） |

定义位置：[`reuleauxcoder/domain/hooks/types.py`](../reuleauxcoder/domain/hooks/types.py:12)

```python
class HookKind(str, Enum):
    GUARD = "guard"
    TRANSFORM = "transform"
    OBSERVER = "observer"
```

### 2.2 HookPoint

当前支持的挂载点：

| HookPoint | 触发时机 | 上下文类型 |
|-----------|----------|------------|
| `BEFORE_TOOL_EXECUTE` | 工具执行前 | `BeforeToolExecuteContext` |
| `AFTER_TOOL_EXECUTE` | 工具执行后 | `AfterToolExecuteContext` |
| `BEFORE_LLM_REQUEST` | LLM 请求前 | `BeforeLLMRequestContext` |
| `AFTER_LLM_RESPONSE` | LLM 响应后 | `AfterLLMResponseContext` |

定义位置：[`reuleauxcoder/domain/hooks/types.py`](../reuleauxcoder/domain/hooks/types.py:20)

```python
class HookPoint(str, Enum):
    BEFORE_TOOL_EXECUTE = "before_tool_execute"
    AFTER_TOOL_EXECUTE = "after_tool_execute"
    BEFORE_LLM_REQUEST = "before_llm_request"
    AFTER_LLM_RESPONSE = "after_llm_response"
```

### 2.3 HookContext

每个 HookPoint 传递结构化的上下文对象：

**基类**：[`HookContext`](../reuleauxcoder/domain/hooks/types.py:30)

```python
@dataclass(slots=True)
class HookContext:
    hook_point: HookPoint
    session_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

**子类**：

- [`BeforeToolExecuteContext`](../reuleauxcoder/domain/hooks/types.py:40)：`tool_call`, `round_index`
- [`AfterToolExecuteContext`](../reuleauxcoder/domain/hooks/types.py:48)：`tool_call`, `result`, `round_index`
- [`BeforeLLMRequestContext`](../reuleauxcoder/domain/hooks/types.py:57)：`request_params`, `messages`, `tools`, `model`
- [`AfterLLMResponseContext`](../reuleauxcoder/domain/hooks/types.py:67)：`request_params`, `response`, `model`

### 2.4 GuardDecision

Guard hook 的决策结果：

定义位置：[`reuleauxcoder/domain/hooks/types.py`](../reuleauxcoder/domain/hooks/types.py:76)

```python
@dataclass(slots=True)
class GuardDecision:
    allowed: bool
    reason: str | None = None
    warning: str | None = None

    @classmethod
    def allow(cls) -> "GuardDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "GuardDecision":
        return cls(allowed=False, reason=reason)

    @classmethod
    def warn(cls, warning: str) -> "GuardDecision":
        return cls(allowed=True, warning=warning)
```

---

## 3. Hook 基类

定义位置：[`reuleauxcoder/domain/hooks/base.py`](../reuleauxcoder/domain/hooks/base.py)

### 3.1 HookBase

所有 Hook 的公共元数据：

```python
@dataclass(slots=True)
class HookBase(Generic[ContextT]):
    name: str
    priority: int = 0
    extension_name: str | None = None
```

### 3.2 GuardHook

```python
class GuardHook(HookBase[ContextT]):
    def run(self, context: ContextT) -> GuardDecision:
        raise NotImplementedError
```

### 3.3 TransformHook

```python
class TransformHook(HookBase[ContextT]):
    def run(self, context: ContextT) -> ContextT:
        raise NotImplementedError
```

**约束**：必须返回同类型 Context，不允许返回 `None`。

### 3.4 ObserverHook

```python
class ObserverHook(HookBase[ContextT]):
    def run(self, context: ContextT) -> None:
        raise NotImplementedError
```

---

## 4. HookRegistry

实例级注册表，管理 Hook 的注册与执行。

定义位置：[`reuleauxcoder/domain/hooks/registry.py`](../reuleauxcoder/domain/hooks/registry.py)

### 4.1 注册与注销

```python
def register(self, hook_point: HookPoint, hook: HookBase[Any]) -> None:
    """注册 Hook 到指定挂载点"""

def unregister(self, hook_point: HookPoint, hook_name: str) -> None:
    """按名称注销 Hook"""

def list_hooks(self, hook_point: HookPoint | None = None) -> dict[str, list[str]]:
    """列出已注册 Hook"""
```

### 4.2 执行方法

```python
def run_guards(self, hook_point: HookPoint, context: HookContext) -> list[GuardDecision]:
    """执行 guard hooks，fail-closed 语义"""

def run_transforms(self, hook_point: HookPoint, context: HookContext) -> HookContext:
    """执行 transform hooks，链式处理"""

def run_observers(self, hook_point: HookPoint, context: HookContext) -> None:
    """执行 observer hooks，fail-open 语义"""
```

### 4.3 执行顺序

Hook 按 `priority` **降序**执行（高优先级先执行）。

### 4.4 克隆

```python
def clone(self) -> "HookRegistry":
    """创建独立的副本，用于 sub-agent 继承父级 Hook 配置"""
```

---

## 5. 执行流程集成

### 5.1 工具执行流程

位置：[`reuleauxcoder/domain/agent/tool_execution.py`](../reuleauxcoder/domain/agent/tool_execution.py:25)

```
ToolExecutor.execute(tool_call):
  1. 创建 BeforeToolExecuteContext
  2. run_guards(BEFORE_TOOL_EXECUTE)
     - 若有拒绝，返回拒绝原因，不执行工具
  3. run_transforms(BEFORE_TOOL_EXECUTE)
     - 可修改 tool_call
  4. run_observers(BEFORE_TOOL_EXECUTE)
  5. 执行工具
  6. 创建 AfterToolExecuteContext
  7. run_transforms(AFTER_TOOL_EXECUTE)
     - 可修改 result
  8. run_observers(AFTER_TOOL_EXECUTE)
  9. 返回结果
```

### 5.2 LLM 请求流程

位置：[`reuleauxcoder/services/llm/client.py`](../reuleauxcoder/services/llm/client.py:39)

```
LLM.chat(messages, tools):
  1. 创建 BeforeLLMRequestContext
  2. run_guards(BEFORE_LLM_REQUEST)
     - 若有拒绝，抛出 RuntimeError
  3. run_transforms(BEFORE_LLM_REQUEST)
     - 可修改 messages/tools/params
  4. run_observers(BEFORE_LLM_REQUEST)
  5. 发送 LLM 请求
  6. 创建 AfterLLMResponseContext
  7. run_transforms(AFTER_LLM_RESPONSE)
     - 可修改 response
  8. run_observers(AFTER_LLM_RESPONSE)
  9. 返回 response
```

**注意**：当前 `BEFORE_LLM_REQUEST` guard 拒绝会直接抛异常中断执行，后续可考虑改为"软拒绝"模式。

---

## 6. Agent 集成

位置：[`reuleauxcoder/domain/agent/agent.py`](../reuleauxcoder/domain/agent/agent.py)

### 6.1 Agent 构造

```python
class Agent:
    def __init__(
        self,
        llm: "LLM",
        tools: Optional[List["Tool"]] = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        hook_registry: HookRegistry | None = None,
    ):
        self.hook_registry = hook_registry or HookRegistry()
```

### 6.2 Hook 注册

```python
def register_hook(self, hook_point: HookPoint, hook: HookBase[object]) -> None:
    """在 agent 级别注册 Hook"""
```

---

## 7. Sub-agent Hook 继承

位置：[`reuleauxcoder/extensions/tools/builtin/agent.py`](../reuleauxcoder/extensions/tools/builtin/agent.py:27)

Sub-agent 创建时会**复制**父 agent 的 hook_registry：

```python
sub = Agent(
    llm=parent.llm,
    tools=[t for t in parent.tools if t.name != "agent"],
    max_context_tokens=parent.context.max_tokens,
    max_rounds=20,
    hook_registry=parent.hook_registry.clone(),
)
```

**语义**：
- 子 agent 继承父级已注册 hooks
- 子 agent 持有独立 registry 副本
- 子 agent 动态注册/注销不影响父级

---

## 8. 内置 Hook

### 8.1 ToolOutputTruncationHook

位置：[`reuleauxcoder/domain/hooks/builtin/tool_output.py`](../reuleauxcoder/domain/hooks/builtin/tool_output.py:14)

**类型**：`TransformHook[AfterToolExecuteContext]`

**功能**：截断过大的工具输出并归档完整内容

**配置参数**：
- `max_chars`：最大字符数
- `max_lines`：最大行数
- `store_full_output`：是否存储完整输出
- `store_dir`：存储目录

**特殊处理**：允许通过 `read_file(override=true)` 读取归档文件

### 8.2 ToolPolicyGuardHook

位置：[`reuleauxcoder/domain/hooks/builtin/tool_policy.py`](../reuleauxcoder/domain/hooks/builtin/tool_policy.py:12)

**类型**：`GuardHook[BeforeToolExecuteContext]`

**功能**：在工具执行前运行配置的 ToolPolicy

**配置参数**：
- `policies`：`tuple[ToolPolicy, ...]`，默认使用 `DEFAULT_TOOL_POLICIES`

---

## 9. Tool Policy 系统

位置：[`reuleauxcoder/extensions/tools/policies/`](../reuleauxcoder/extensions/tools/policies)

### 9.1 ToolPolicy 抽象

位置：[`reuleauxcoder/extensions/tools/policies/base.py`](../reuleauxcoder/extensions/tools/policies/base.py:11)

```python
class ToolPolicy(Protocol):
    def evaluate(self, tool_call: ToolCall) -> GuardDecision | None:
        """返回决策当 policy 适用，否则返回 None"""
```

### 9.2 BashDangerousCommandPolicy

位置：[`reuleauxcoder/extensions/tools/policies/bash.py`](../reuleauxcoder/extensions/tools/policies/bash.py:12)

**功能**：拦截危险 shell 命令

**规则**：
- 递归删除 home/root
- 强制递归删除
- 格式化文件系统
- 原始磁盘写入
- 覆盖块设备
- chmod 777 on root
- fork bomb
- pipe curl/wget to bash

### 9.3 默认 Policy 集合

```python
DEFAULT_TOOL_POLICIES: tuple[ToolPolicy, ...] = (
    BashDangerousCommandPolicy(),
)
```

---

## 10. CLI 启动注册

位置：[`reuleauxcoder/interfaces/cli/main.py`](../reuleauxcoder/interfaces/cli/main.py:55)

```python
agent.register_hook(
    HookPoint.BEFORE_TOOL_EXECUTE,
    ToolPolicyGuardHook(priority=100),
)
agent.register_hook(
    HookPoint.AFTER_TOOL_EXECUTE,
    ToolOutputTruncationHook(
        max_chars=config.tool_output_max_chars,
        max_lines=config.tool_output_max_lines,
        store_full_output=config.tool_output_store_full,
        store_dir=config.tool_output_store_dir,
        priority=0,
    ),
)
```

---

## 11. 配置支持

位置：[`reuleauxcoder/domain/config/schema.py`](../reuleauxcoder/domain/config/schema.py)

```yaml
tool_output:
  max_chars: 12000
  max_lines: 120
  store_full_output: true
  store_dir: ./.rcoder/tool-outputs
```

---

## 12. 扩展指南

### 12.1 创建自定义 Hook

**Guard Hook 示例**：

```python
from reuleauxcoder.domain.hooks.base import GuardHook
from reuleauxcoder.domain.hooks.types import BeforeToolExecuteContext, GuardDecision

class MyToolGuard(GuardHook[BeforeToolExecuteContext]):
    def __init__(self, priority: int = 0):
        super().__init__(name="my_tool_guard", priority=priority)
    
    def run(self, context: BeforeToolExecuteContext) -> GuardDecision:
        if context.tool_call and context.tool_call.name == "bash":
            # 检查逻辑
            return GuardDecision.deny("理由")
        return GuardDecision.allow()
```

**Transform Hook 示例**：

```python
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext

class MyResultTransform(TransformHook[AfterToolExecuteContext]):
    def __init__(self, priority: int = 0):
        super().__init__(name="my_result_transform", priority=priority)
    
    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        # 修改 context.result
        context.result = "处理后的结果"
        return context
```

### 12.2 创建自定义 Policy

```python
from reuleauxcoder.extensions.tools.policies.base import ToolPolicy
from reuleauxcoder.domain.hooks.types import GuardDecision
from reuleauxcoder.domain.llm.models import ToolCall

class MyToolPolicy(ToolPolicy):
    def evaluate(self, tool_call: ToolCall) -> GuardDecision | None:
        if tool_call.name != "my_tool":
            return None
        
        # 检查逻辑
        return GuardDecision.deny("理由")
```

### 12.3 注册 Hook

```python
agent.register_hook(HookPoint.BEFORE_TOOL_EXECUTE, MyToolGuard(priority=50))
```

---

## 13. 设计决策记录

| 决策 | 状态 | 说明 |
|------|------|------|
| Hook 分三类 | 已确认 | guard / transform / observer |
| payload 采用 context 对象 | 已确认 | 不使用裸 dict/str |
| transform 必须返回 payload | 已确认 | 不允许 None |
| 异常策略按类型区分 | 已确认 | guard fail-closed，observer fail-open |
| 支持 priority | 已确认 | 降序执行 |
| 实例级 Registry | 已确认 | 不使用全局注册表 |
| Sub-agent 继承 Hook | 已确认 | 通过 clone() 复制 |
| Policy 作为 Hook 数据源 | 已确认 | ToolPolicyGuardHook 消费 Policy |

---

## 14. 后续扩展点

以下属于未来工作，不纳入当前阶段：

1. **LLM Guard 软拒绝**：改为返回结构化结果而非抛异常
2. **Hook 传播策略**：给 Hook 增加 `propagate_to_subagents` 标记
3. **更多 Policy**：文件路径 Policy、递归深度 Policy、工具白名单 Policy
4. **异步 Hook**：当前只支持同步
5. **更多 HookPoint**：会话保存前后、上下文压缩前后等
