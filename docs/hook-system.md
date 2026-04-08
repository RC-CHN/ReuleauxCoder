# Hook 系统设计（RFC 草案）

## 1. 文档定位

本文档定义 ReuleauxCoder 的 Hook 系统设计目标、边界、核心抽象与落地计划。

这是一份**面向实现的设计文档**，不是伪代码备忘录。文中会区分：

- **已确认的设计决策**：本轮已经拍板，后续实现应遵循
- **建议的实现形态**：作为第一版实现方向
- **后续扩展点**：明确属于未来工作，不纳入第一阶段

---

## 2. 为什么需要 Hook

当前项目已经存在事件体系：

- 领域层事件：[`AgentEvent`](../reuleauxcoder/domain/agent/events.py:23)
- 接口层事件总线：[`UIEventBus`](../reuleauxcoder/interfaces/events.py:95)
- UI 事件设计文档：[`docs/ui-event-system.md`](docs/ui-event-system.md)

这些机制已经能够很好地支持：

- 生命周期通知
- UI 输出桥接
- 观测与日志
- 流式渲染

但它们**不适合承担“介入执行点并影响执行数据”的职责**。例如：

- 在调用 LLM 前补充请求上下文
- 在工具执行前做守卫检查
- 在工具执行后统一处理结果
- 在会话保存前清洗或追加元信息

因此需要引入 Hook 系统。

### 2.1 Hook 与 Event 的边界

这是本设计最重要的前提。

#### Event

Event 用于：

- 表示系统已经发生的事情
- 通知外部观察者
- 驱动 UI / 日志 / telemetry

Event **默认不修改主流程数据**。

#### Hook

Hook 用于：

- 介入某个明确的执行点
- 读取上下文
- 在允许的场景下修改 payload
- 在允许的场景下阻止继续执行

Hook **是 execution interception 机制，不是 notification 机制**。

### 2.2 设计结论

- Event 不替代 Hook
- Hook 不替代 Event
- 二者并存，各司其职

可简化为一句话：

> Event 负责“发生了什么”，Hook 负责“在发生之前或之后如何介入”。

---

## 3. 设计目标

Hook 系统第一版需要满足以下目标：

1. **介入明确的执行点**
2. **支持结构化上下文对象，而非裸 `dict` / `str`**
3. **支持三类 Hook：guard / transform / observer**
4. **支持优先级与稳定顺序**
5. **支持实例级注册表 [`HookRegistry`](docs/hook-system.md)**
6. **与现有事件体系协同工作**
7. **第一版只做同步执行**
8. **不承担 Mode 等核心安全能力的兜底职责**

非目标：

- 第一版不做完整插件框架重构
- 第一版不做异步 Hook
- 第一版不做 Mode 系统落地
- 第一版不做所有执行点全覆盖

---

## 4. 范围结论：完整设计，分阶段实现

这里需要先明确一个产品/工程结论：

### 4.1 架构上应该先把 Hook 设计完整

文档层面应把以下内容一次性定义清楚：

- Hook 的角色
- 三类 Hook 抽象
- 上下文模型
- 返回值协议
- 顺序与优先级
- 异常语义
- 与事件系统的关系
- 与 extensions 的关系

原因是这些内容一旦实现后再改，兼容成本会很高。

### 4.2 实现上不应该第一版把所有 Hook 点全做完

虽然设计可以完整，但工程实现建议按 MVP 推进。

### 4.3 结论

> **设计做完整，实施做最小闭环。**

也就是说：

- 文档先把体系定完整
- 代码先做最核心的一小组 Hook 点

推荐的第一阶段范围见后文“实施计划”。

---

## 5. 核心术语

### 5.1 Hook Point

Hook Point 表示一个可被介入的执行点，例如：

- `before_tool_execute`
- `after_tool_execute`
- `before_llm_request`

### 5.2 Hook Context

每个 Hook Point 都会传递一个结构化的 context 对象。Hook 不直接操作裸字符串或任意 `dict`。

### 5.3 Hook Kind

Hook 分为三种语义类别：

- `guard`
- `transform`
- `observer`

### 5.4 Hook Registry

[`HookRegistry`](docs/hook-system.md) 是一个实例级注册表，用于保存已注册的 Hook，并按规则执行。

注意：第一版**不使用全局注册表**。

### 5.5 Extensions

项目当前术语应使用 **extensions**，而不是 plugin。

Hook 系统的定位是：

- ReuleauxCoder 核心提供 Hook runtime
- 各类 extensions 可以注册 Hook

---

## 6. 核心设计决策

本节列出已经确认的设计决策。

### 6.1 Hook 是“介入执行点”的机制

已确认。

### 6.2 Hook 分三类

已确认：

- guard hook
- transform hook
- observer hook

### 6.3 Mode 是核心能力，不是通过 Hook 挂上去的

已确认。

Mode 可以在将来与 Hook 协作，但其权限控制和安全边界应属于核心执行路径，而不是依赖可选 Hook。

### 6.4 payload 采用 context 对象基类

已确认。

Hook payload 不再使用“`str` 或 `dict` 视情况而定”的松散协议，而是统一基于上下文对象。

### 6.5 三类 Hook 分别采用不同返回协议

已确认。

### 6.6 transform hook 必须返回 payload

已确认。

这意味着 transform hook 不允许用 `None` 表示“不修改”。

### 6.7 异常策略按 Hook 类型区分

已确认。

详见后文“异常语义”。

### 6.8 Hook 支持 priority

已确认。

### 6.9 默认执行顺序

已确认：

- 同一执行阶段内：`guard -> transform -> observer`
- 同类 Hook 内：按优先级排序执行

### 6.10 第一版先做同步

已确认。

### 6.11 使用实例级 [`HookRegistry`](docs/hook-system.md)

已确认。

### 6.12 术语使用 extensions，不使用 plugin

已确认。

### 6.13 先把 Hook 做好，再做 Mode

已确认。

### 6.14 关键执行点的统一流程

已确认：

1. emit `start event`
2. run guard / transform hooks
3. 执行实际动作
4. run after hooks
5. emit `end event` 或 `error event`

---

## 7. 架构概览

```text
Domain / Service execution flow
        │
        ├─ emit start event
        │
        ├─ HookRegistry.run_guard(...)
        ├─ HookRegistry.run_transform(...)
        ├─ HookRegistry.run_observer(... optional before-observers)
        │
        ├─ execute actual action
        │
        ├─ HookRegistry.run_guard(... optional after-guards, usually rare)
        ├─ HookRegistry.run_transform(... after-transform if allowed)
        ├─ HookRegistry.run_observer(... after-observers)
        │
        └─ emit end event / error event
```

说明：

- “before” 与 “after” 是两个不同 Hook Point
- 一般情况下，guard 主要用于“before”阶段
- after 阶段主要以 transform / observer 为主
- 事件始终作为观测与桥接机制保留

---

## 8. Hook 类型系统

### 8.1 三类 Hook 的职责

#### Guard Hook

职责：

- 检查是否允许继续执行
- 不负责修改 payload
- 可以返回 allow / deny / warn 等决策

适用场景：

- 工具调用前策略检查
- LLM 请求前策略限制
- 资源访问前安全审计

#### Transform Hook

职责：

- 修改 context 中允许修改的 payload
- 链式执行
- 必须返回同类型 context

适用场景：

- 注入额外上下文
- 清洗请求参数
- 标准化工具结果

#### Observer Hook

职责：

- 只观察，不修改主流程语义
- 返回 `None`
- 不影响主流程推进

适用场景：

- 日志
- 审计
- 统计
- tracing

### 8.2 建议的基类设计

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar, Optional

ContextT = TypeVar("ContextT", bound="HookContext")


@dataclass
class HookContext:
    """所有 hook context 的基类。"""

    hook_point: str
    session_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class HookBase(Generic[ContextT]):
    """所有 hook 的公共基类。"""

    name: str
    priority: int = 0
    extension_name: str | None = None


class GuardHook(HookBase[ContextT]):
    def run(self, context: ContextT) -> "GuardDecision":
        raise NotImplementedError


class TransformHook(HookBase[ContextT]):
    def run(self, context: ContextT) -> ContextT:
        raise NotImplementedError


class ObserverHook(HookBase[ContextT]):
    def run(self, context: ContextT) -> None:
        raise NotImplementedError
```

设计要点：

- 三类 Hook 有清晰的基类边界
- 统一携带 `name`、`priority`、`extension_name`
- 各类 Hook 的返回值协议在类型层面就不同

---

## 9. Context 模型

### 9.1 基本原则

所有 Hook Point 的 payload 都应是 [`HookContext`](docs/hook-system.md) 子类。

原则：

1. 使用结构化对象，不使用裸值
2. 区分可修改字段与只读上下文
3. 允许带 `metadata`
4. 允许逐步扩展字段，但保持兼容

### 9.2 建议的 context 基类

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HookContext:
    hook_point: str
    session_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 9.3 示例：工具执行前 context

```python
from dataclasses import dataclass
from reuleauxcoder.domain.llm.models import ToolCall


@dataclass
class BeforeToolExecuteContext(HookContext):
    tool_call: ToolCall
    mode_name: str | None = None
```

### 9.4 示例：工具执行后 context

```python
from dataclasses import dataclass
from reuleauxcoder.domain.llm.models import ToolCall


@dataclass
class AfterToolExecuteContext(HookContext):
    tool_call: ToolCall
    result: str
```

### 9.5 示例：LLM 请求前 context

```python
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BeforeLLMRequestContext(HookContext):
    request_params: dict[str, Any] = field(default_factory=dict)
```

### 9.6 为什么不用裸 `dict`

使用 context 子类的原因：

- 类型更稳定
- 调试更容易
- 字段更明确
- 更容易在未来增加 tracing / source info / mode info
- 不会出现“这个 Hook 点到底传 `str` 还是 `dict`”的歧义

---

## 10. 返回值协议

### 10.1 Guard Hook 返回值

Guard Hook 返回显式决策对象，而不是普通 `dict`。

```python
from dataclasses import dataclass


@dataclass
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

说明：

- `allow`：通过
- `deny`：阻止继续执行
- `warn`：允许继续，但附带警告信息

### 10.2 Transform Hook 返回值

Transform Hook：

- **必须返回 context 本身或修改后的同类型 context**
- 不允许返回 `None`
- 不允许返回其他结构

这条必须严格执行，否则 transform 链会变得脆弱。

### 10.3 Observer Hook 返回值

Observer Hook 返回：

- `None`

Observer Hook 不参与主流程数据变换。

---

## 11. 执行顺序与优先级

### 11.1 类别顺序

在同一个 Hook Point 中，执行顺序固定为：

1. `guard`
2. `transform`
3. `observer`

### 11.2 同类 Hook 顺序

同类 Hook 内按以下规则排序：

1. `priority` 高的先执行
2. 相同 `priority` 时按注册顺序稳定执行

### 11.3 设计原因

采用 `guard -> transform -> observer` 的原因：

- 先决定能不能继续
- 再修改实际 payload
- 最后做纯观察行为

这样语义最稳定，排错也最容易。

---

## 12. 异常语义

异常策略必须按 Hook 类型区分，不能“一刀切”。

### 12.1 Guard Hook

默认策略：**fail-closed**。

含义：

- 如果 guard hook 自身异常，默认视为拒绝继续执行
- 原因应进入错误日志 / 审计信息

原因：

- guard hook 本质上是策略检查
- 发生异常时继续放行通常更危险

### 12.2 Transform Hook

默认策略：**异常冒泡并终止当前执行点**。

含义：

- 某个 transform hook 出错，主流程视为失败
- 不应静默吞掉异常

原因：

- transform hook 会修改主流程数据
- 静默失败会导致后续状态不确定

### 12.3 Observer Hook

默认策略：**fail-open**。

含义：

- observer hook 异常时记录错误
- 不阻断主流程

原因：

- observer hook 主要承担日志、统计、审计等附加职责
- 不应因为观测失败影响主流程

### 12.4 禁止的做法

第一版明确禁止：

```python
except Exception:
    pass
```

所有 Hook 异常至少都应带：

- hook point
- hook name
- extension name
- 异常信息

---

## 13. HookRegistry 设计

### 13.1 设计原则

[`HookRegistry`](docs/hook-system.md) 采用**实例级**设计，而不是全局单例。

原因：

- 更容易测试
- 更容易隔离不同运行时
- 更容易在未来做热重载
- 避免全局状态污染

### 13.2 生命周期与挂载位置

已确认：[`HookRegistry`](docs/hook-system.md) 挂在 [`Agent`](../reuleauxcoder/domain/agent/agent.py:27) 上，由 Agent 作为 runtime 聚合根统一持有。

这样做的原因是：

- [`Agent`](../reuleauxcoder/domain/agent/agent.py:27) 已经统一持有 LLM、tools、context manager、executor 与 event handlers
- Hook runtime 与一次 agent 运行实例的生命周期天然一致
- 多个 agent 实例之间不会共享可变 hook 状态
- 入口装配更简单，便于测试隔离

### 13.3 建议接口

```python
from collections import defaultdict
from typing import Any


class HookRegistry:
    def __init__(self):
        self._hooks: dict[str, list[HookBase[Any]]] = defaultdict(list)

    def register(self, hook_point: str, hook: HookBase[Any]) -> None:
        self._hooks[hook_point].append(hook)

    def unregister(self, hook_point: str, hook_name: str) -> None:
        self._hooks[hook_point] = [
            h for h in self._hooks[hook_point] if h.name != hook_name
        ]

    def list_hooks(self, hook_point: str | None = None) -> dict[str, list[str]]:
        if hook_point is not None:
            return {hook_point: [h.name for h in self._hooks.get(hook_point, [])]}
        return {point: [h.name for h in hooks] for point, hooks in self._hooks.items()}
```

### 13.4 执行接口建议

建议不要只有一个“万能 [`run_hooks()`](docs/hook-system.md)”接口，而是显式区分：

```python
class HookRegistry:
    def run_guards(self, hook_point: str, context: HookContext) -> list[GuardDecision]:
        ...

    def run_transforms(self, hook_point: str, context: HookContext) -> HookContext:
        ...

    def run_observers(self, hook_point: str, context: HookContext) -> None:
        ...
```

原因：

- 不同 Hook 类型返回值不同
- 异常策略不同
- 混在一个入口里容易变得模糊

---

## 14. 与 extensions 的关系

### 14.1 术语统一

项目中统一使用 **extensions**。

不再使用 plugin 作为主术语。

### 14.2 关系定义

- **extension**：扩展分发与装配单位
- **hook**：extension 可注册的一种运行时介入机制

这意味着：

- 一个 extension 可以注册若干 hook
- 一个 extension 也可以注册 tool、MCP 适配器等其他扩展能力

### 14.3 extension 生命周期

第一版建议：

- 应用启动时创建 [`HookRegistry`](docs/hook-system.md)
- 核心加载 extension
- extension 在初始化阶段向 registry 注册 hook
- 执行器持有 registry 引用

后续可考虑：

- extension 卸载
- hook 热重载
- extension 级配置刷新

---

## 15. 与 Mode 的关系

Mode 系统不是 Hook 系统的一部分。

### 15.1 结论

- Mode 是核心能力
- Hook 不是 Mode 的实现载体
- Hook 最多用于给 Mode 提供扩展信息或附加限制

### 15.2 当前阶段策略

当前阶段：

- 先实现 Hook runtime
- 先不落地 Mode 系统

这是为了避免同时引入过多耦合项：

- hook runtime
- mode manager
- permission model
- config schema
- CLI 交互

Mode 放到 Hook 稳定后再做更合理。

---

## 16. 关键执行点的统一流程

这一部分已经确认，应作为整个 Hook 系统的统一约束。

### 16.1 标准流程

对任意支持 Hook 的关键执行点，建议遵循：

1. emit `start event`
2. run `guard hooks`
3. run `transform hooks`
4. 执行实际动作
5. run `after hooks`
6. emit `end event` 或 `error event`

### 16.2 说明

其中：

- before 阶段主要用于 guard / transform
- after 阶段主要用于 transform / observer
- observer hook 可用于 before 和 after，但它不能改变主流程语义

### 16.3 工具执行示意

```text
emit TOOL_CALL_START
↓
run before_tool_execute guards
↓
run before_tool_execute transforms
↓
execute tool
↓
run after_tool_execute transforms
↓
run after_tool_execute observers
↓
emit TOOL_CALL_END / ERROR
```

### 16.4 LLM 请求示意

```text
emit request-start event
↓
run before_llm_request guards
↓
run before_llm_request transforms
↓
send request to LLM
↓
run after_llm_response transforms
↓
run after_llm_response observers
↓
emit response-end / error event
```

---

## 17. 第一阶段建议支持的 Hook Point

虽然体系设计完整，但第一版不建议把所有 Hook Point 一次实现完。

### 17.1 推荐 MVP

推荐第一阶段只实现以下 Hook Point：

1. `before_tool_execute`
2. `after_tool_execute`
3. `before_llm_request`
4. `after_llm_response`

### 17.2 为什么是这四个

因为它们：

- 价值高
- 主流程明确
- 容易验证 Hook 机制是否成立
- 能覆盖三类典型需求：守卫、变换、观察

### 17.3 暂缓的 Hook Point

以下点位先不纳入第一阶段：

- 会话恢复
- 会话保存/加载
- MCP server 连接/断开
- app start / app exit
- mode switch

这些执行点可以等 Hook runtime 稳定后再逐步扩展。

---

## 18. Hook Point 候选全集（未来扩展）

本节是候选全集，不代表第一阶段全部实现。

### 18.1 用户输入流程

| Hook Point | 说明 | 第一阶段 |
|-----------|------|----------|
| `before_user_message` | 用户输入进入 Agent 前 | 否 |
| `after_user_message` | 用户输入处理完成后 | 否 |

### 18.2 LLM 流程

| Hook Point | 说明 | 第一阶段 |
|-----------|------|----------|
| `before_llm_request` | 请求发往 LLM 前 | 是 |
| `after_llm_response` | LLM 响应返回后 | 是 |
| `on_llm_error` | LLM 请求失败时 | 否 |

### 18.3 工具执行流程

| Hook Point | 说明 | 第一阶段 |
|-----------|------|----------|
| `before_tool_execute` | 工具执行前 | 是 |
| `after_tool_execute` | 工具执行后 | 是 |
| `on_tool_error` | 工具执行失败时 | 否 |

### 18.4 会话流程

| Hook Point | 说明 | 第一阶段 |
|-----------|------|----------|
| `before_session_save` | 会话保存前 | 否 |
| `after_session_load` | 会话加载后 | 否 |
| `on_session_resume` | 恢复会话首条消息前 | 否 |

### 18.5 其他流程

| Hook Point | 说明 | 第一阶段 |
|-----------|------|----------|
| `on_mcp_server_connect` | MCP server 连接后 | 否 |
| `on_mcp_server_disconnect` | MCP server 断开后 | 否 |
| `on_app_start` | 应用启动后 | 否 |
| `on_app_exit` | 应用退出前 | 否 |

---

## 19. 配置策略

第一版配置设计应保持克制。

### 19.1 当前结论

- Hook 系统先落 runtime
- 不要求第一版立即引入复杂的 extension 配置模型
- Mode 配置暂不进入当前阶段

### 19.2 建议的最小配置能力

未来可按以下两层设计：

#### 核心配置

用于控制：

- 启用哪些 extensions
- 是否开启 Hook tracing
- 是否开启 debug 输出

#### extension 私有配置

采用透传字典：

```yaml
extensions:
  enabled:
    - hooks.audit
    - hooks.prompt_enricher
  config:
    hooks.audit:
      level: info
    hooks.prompt_enricher:
      timezone: Asia/Shanghai
```

这里仍然使用 `extensions` 术语，而不是 `plugins`。

### 19.3 当前不做的配置项

当前阶段暂不设计：

- 完整 mode schema
- hook 热重载配置
- 复杂的 extension 生命周期控制

---

## 20. 可观测性与调试要求

Hook 系统如果缺少可观测性，会非常难排查问题。

第一版建议至少满足：

1. 可以列出已注册 hook
2. hook 执行失败时能输出结构化错误信息
3. debug 模式下可看到 hook 执行轨迹
4. 日志中包含 `hook_point` / `hook_name` / `extension_name`

建议的调试信息：

- hook point
- hook kind
- hook name
- priority
- extension name
- 执行耗时
- 执行结果（allow / deny / transformed / observed / failed）

---

## 21. 测试要求

第一版实现时建议至少覆盖：

1. 注册与取消注册
2. priority 排序是否正确
3. 同优先级是否稳定
4. `guard -> transform -> observer` 顺序是否正确
5. guard deny 时是否阻止执行
6. transform 链是否正确传递 context
7. observer 异常是否不会中断主流程
8. transform 异常是否会中断执行
9. registry 是否为实例级、相互隔离

---

## 22. 建议的最小代码骨架

以下为建议性的第一版骨架，不代表最终文件路径必须完全一致。

```text
reuleauxcoder/
├── extensions/
│   └── hooks/
│       ├── __init__.py
│       ├── types.py         # 纯类型定义：HookPoint / HookKind / Context / Decision
│       ├── base.py          # Hook 抽象基类
│       ├── registry.py      # HookRegistry
│       └── builtin/
│           └── ...
```

### 22.1 建议职责划分

- `types.py`：纯类型定义，包含 hook point、hook kind、context 基类与具体 context、guard decision 等
- `base.py`：三类 Hook 抽象基类，不放 registry 行为
- `registry.py`：注册、排序、执行逻辑
- `builtin/`：内建 hook 实现（如果需要）

这样拆分的原因是：

- 类型定义最稳定，应该独立收口
- registry 行为与类型模型解耦，更容易测试
- 后续具体 hook 实现只需要依赖 [`types.py`](../reuleauxcoder/extensions/hooks/types.py) 和 [`base.py`](../reuleauxcoder/extensions/hooks/base.py)

---

## 23. 并行工具执行语义

第一版虽然只支持同步 Hook runtime，但**不要求为了接入 Hook 而取消现有的并行工具执行**。

当前并行执行发生在：

- [`ToolExecutor.execute_parallel()`](../reuleauxcoder/domain/agent/tool_execution.py:42)
- [`AgentLoop.run()`](../reuleauxcoder/domain/agent/loop.py:28)

### 23.1 设计结论

并行工具执行场景下，遵循以下约束：

1. [`HookRegistry`](docs/hook-system.md) 在同一个 [`Agent`](../reuleauxcoder/domain/agent/agent.py:27) 实例内共享
2. 每个 tool call 必须拥有**独立的 context 实例**
3. worker 线程不得直接修改 [`AgentState.messages`](../reuleauxcoder/domain/agent/agent.py:21)
4. 最终结果由主线程统一聚合并按原 tool call 顺序写回
5. 第一版只支持 **per-call hooks**，不支持 batch-level hook

### 23.2 什么是“同步 Hook runtime”

“同步”指的是：

- 单个 tool call 内部的 hook 执行是同步的
- 单个 hook 本身不是 async hook
- `guard -> transform -> observer` 在单次调用内按顺序执行

它**不等于**整个工具批次必须串行执行。

### 23.3 共享什么，不共享什么

#### 可以共享的对象

- [`HookRegistry`](docs/hook-system.md)
- 只读配置
- 只读 extension 元数据
- 工具实例（前提是工具自身线程安全）

#### 必须独立的对象

- `BeforeToolExecuteContext`
- `AfterToolExecuteContext`
- `GuardDecision`
- 每次调用的 trace / execution record
- transform 链中的中间结果

### 23.4 顺序保证

并行场景下，只保证以下顺序：

#### 单个 tool call 内部

严格保证：

1. before guards
2. before transforms
3. execute tool
4. after transforms
5. after observers

#### 多个 tool call 之间

不保证不同 tool call 之间 hook 的完成先后顺序。

系统只保证：

- 每个 tool call 自己内部的 hook 顺序
- 最终结果按原 tool call 顺序聚合回消息历史

### 23.5 为什么第一版不做 batch-level hook

因为 batch-level hook 需要看到整个工具调用集合，例如：

- 本轮最多允许几个工具并行执行
- 哪些工具组合不能同时出现
- 多个调用之间是否共享限额

这类策略不属于第一版最小闭环范围。

第一版只支持**单调用级别 hook**，例如：

- 检查当前工具名是否允许
- 补充当前工具的默认参数
- 标准化当前工具的输出结果

如果未来需要批量级策略，可再引入独立的 Hook Point，例如 `before_tool_batch_execute`。

### 23.6 实施约束

并行工具执行接入 Hook 时，应遵守：

- registry 在执行期应尽量保持只读访问
- context 为每次调用单独创建，不复用
- observer hook 不应直接修改共享普通 `dict` / `list`
- worker 线程只返回结果与必要 trace，由主线程统一汇总

一句话总结：

> 并行不是问题，共享可变状态才是问题。Hook 系统通过“共享 registry、独立 context、主线程聚合结果”的策略与现有并行工具执行兼容。

---

## 24. 与现有代码的落点建议

### 23.1 工具执行路径

工具执行目前位于 [`reuleauxcoder/domain/agent/tool_execution.py`](../reuleauxcoder/domain/agent/tool_execution.py)。

它应成为第一阶段最优先接入 Hook 的位置。

建议流程：

- 发出工具调用开始事件
- 构建 `BeforeToolExecuteContext`
- 跑 before guards / transforms
- 执行工具
- 构建 `AfterToolExecuteContext`
- 跑 after transforms / observers
- 发出结束或错误事件

### 23.2 LLM 请求路径

LLM 请求目前位于 [`reuleauxcoder/services/llm/client.py`](../reuleauxcoder/services/llm/client.py)。

它应成为第二个接入点。

建议流程：

- 构建 `BeforeLLMRequestContext`
- 跑 guards / transforms
- 发送请求
- 构建 `AfterLLMResponseContext`
- 跑 transforms / observers

### 23.3 事件体系保持不变

现有：

- [`AgentEvent`](../reuleauxcoder/domain/agent/events.py:23)
- [`UIEventBus`](../reuleauxcoder/interfaces/events.py:95)

都应保留，不应被 Hook 替代。

---

## 25. 暂不采纳的方案

以下方案当前明确不采纳。

### 24.1 全局 `HOOK_REGISTRY`

不采纳原因：

- 测试污染
- 生命周期不清晰
- 不利于未来热重载
- 对并发场景不友好

### 24.2 万能 `run_hooks(point, data)`

不采纳原因：

- 三类 Hook 返回值不同
- 错误处理不同
- 类型边界不清楚

### 24.3 以普通 `dict` 作为 guard 返回值

不采纳原因：

- 协议脆弱
- 不可静态检查
- 可读性差

### 24.4 transform hook 返回 `None` 表示不修改

不采纳原因：

- 会破坏 transform 链
- 语义不明确

### 24.5 使用 Hook 实现 Mode 核心能力

不采纳原因：

- Mode 应属于核心执行逻辑
- 安全边界不能依赖可选扩展

---

## 26. 实施计划

### 阶段 1：Hook runtime MVP

目标：建立最小可用 Hook 基础设施。

范围：

- [`HookContext`](docs/hook-system.md) 基类
- Guard / Transform / Observer 基类
- [`GuardDecision`](docs/hook-system.md)
- [`HookRegistry`](docs/hook-system.md)
- priority 排序
- 同步执行
- 工具执行路径接入
- LLM 请求路径接入

### 阶段 2：内建 hook 与 tracing

目标：让系统可调试、可观测。

范围：

- hook trace 输出
- hook registry 列举能力
- 若干简单内建 extensions hook

### 阶段 3：扩展更多 Hook Point

范围：

- session save/load/resume
- MCP 生命周期
- app 生命周期

### 阶段 4：Mode 与策略系统

前提：

- Hook runtime 已稳定
- 配置模型已准备好
- 执行路径的边界已足够清晰

---

## 27. 最终结论

本次设计已经确认以下方向：

- Hook 是**介入执行点**的机制
- Hook 分为 **guard / transform / observer** 三类
- 使用 **context 对象基类** 作为 payload
- Guard / Transform / Observer 使用**不同返回协议**
- Transform hook **必须返回 payload**
- 异常策略按 Hook 类型区分
- 支持 **priority**，并按 `guard -> transform -> observer` 执行
- 第一版先做**同步**
- 采用实例级 **[`HookRegistry`](docs/hook-system.md)**
- 项目术语统一使用 **extensions**
- **先做 Hook，再做 Mode**
- 核心执行点统一采用：
  - emit start event
  - run guard / transform hooks
  - 执行实际动作
  - run after hooks
  - emit end / error event

关于“该先做完整还是先做完整”的结论也明确如下：

> **文档设计要完整，工程实现要先做 MVP。**

这是当前最稳妥、也最容易落地的路线。
