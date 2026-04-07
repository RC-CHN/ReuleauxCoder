# UI 事件系统与视图逻辑分离

本文档介绍 ReuleauxCoder 的 UI 事件系统设计，以及如何通过事件驱动实现视图层与逻辑层的解耦。

## 设计目标

1. **视图与逻辑分离**：业务逻辑层不直接依赖任何 UI 实现
2. **事件驱动**：所有用户可见输出通过事件系统传递
3. **可扩展性**：支持多种前端实现（CLI、TUI、Web API 等）
4. **统一收口**：所有输出收敛到接口层

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                      Domain Layer                           │
│  Agent → AgentEvent → AgentEventBridge → UIEventBus         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    Extensions Layer                         │
│  MCPClient/MCPManager → UIEventBus                          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                     Interface Layer                         │
│  UIEventBus → CLIRenderer.on_ui_event() → console/print     │
└─────────────────────────────────────────────────────────────┘
```

## 两层事件系统

### Agent 事件（领域层）

Agent 事件用于描述 Agent 运行时生命周期，定义在 `reuleauxcoder/domain/agent/events.py`。

#### AgentEventType

| 事件类型 | 说明 |
|---------|------|
| `CHAT_START` | 聊天开始 |
| `CHAT_END` | 聊天结束 |
| `STREAM_TOKEN` | 流式 token |
| `TOOL_CALL_START` | 工具调用开始 |
| `TOOL_CALL_END` | 工具调用结束 |
| `COMPRESSION_START` | 上下文压缩开始 |
| `COMPRESSION_END` | 上下文压缩结束 |
| `ERROR` | 错误 |

#### AgentEvent

```python
@dataclass
class AgentEvent:
    event_type: AgentEventType
    timestamp: float
    data: dict
    
    # 工具调用相关字段
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[str] = None
    
    # 错误相关字段
    error_message: Optional[str] = None
```

#### 工厂方法

```python
# 创建聊天开始事件
AgentEvent.chat_start(user_input: str) -> AgentEvent

# 创建聊天结束事件
AgentEvent.chat_end(response: str) -> AgentEvent

# 创建流式 token 事件
AgentEvent.stream_token(token: str) -> AgentEvent

# 创建工具调用开始事件
AgentEvent.tool_call_start(tool_name: str, tool_args: dict) -> AgentEvent

# 创建工具调用结束事件
AgentEvent.tool_call_end(tool_name: str, result: str) -> AgentEvent

# 创建错误事件
AgentEvent.error(message: str) -> AgentEvent
```

### UI 事件（接口层）

UI 事件用于描述用户可见的通知和状态，定义在 `reuleauxcoder/interfaces/events.py`。

#### UIEventLevel

| 级别 | 说明 | CLI 渲染样式 |
|-----|------|-------------|
| `INFO` | 普通信息 | 默认样式 |
| `SUCCESS` | 成功信息 | 绿色 |
| `WARNING` | 警告信息 | 黄色 |
| `ERROR` | 错误信息 | 红色 |
| `DEBUG` | 调试信息 | 灰色 |

#### UIEventKind

| 类型 | 说明 |
|-----|------|
| `SYSTEM` | 系统级通知 |
| `COMMAND` | 命令执行结果 |
| `SESSION` | 会话相关通知 |
| `MCP` | MCP 扩展相关 |
| `AGENT` | Agent 事件桥接 |

#### UIEvent

```python
@dataclass
class UIEvent:
    message: str
    level: UIEventLevel = UIEventLevel.INFO
    kind: UIEventKind = UIEventKind.SYSTEM
    timestamp: float
    data: dict
```

#### 工厂方法

```python
# 创建信息事件
UIEvent.info(message: str, *, kind: UIEventKind, **data) -> UIEvent

# 创建成功事件
UIEvent.success(message: str, *, kind: UIEventKind, **data) -> UIEvent

# 创建警告事件
UIEvent.warning(message: str, *, kind: UIEventKind, **data) -> UIEvent

# 创建错误事件
UIEvent.error(message: str, *, kind: UIEventKind, **data) -> UIEvent

# 创建调试事件
UIEvent.debug(message: str, *, kind: UIEventKind, **data) -> UIEvent
```

## UIEventBus

UI 事件总线是同步的发布/订阅系统，所有 UI 输出都通过它传递。

### 订阅事件

```python
from reuleauxcoder.interfaces.events import UIEventBus

bus = UIEventBus()

def my_handler(event: UIEvent) -> None:
    print(f"[{event.level.value}] {event.message}")

bus.subscribe(my_handler)
```

### 发布事件

```python
# 使用便捷方法
bus.info("普通信息")
bus.success("操作成功")
bus.warning("警告信息")
bus.error("错误信息")
bus.debug("调试信息")

# 指定事件类型
bus.success("Session saved: abc123", kind=UIEventKind.SESSION)
bus.error("MCP connection failed", kind=UIEventKind.MCP)

# 直接发布事件
bus.emit(UIEvent.info("自定义事件", kind=UIEventKind.SYSTEM))
```

## AgentEventBridge

AgentEventBridge 将领域层的 Agent 事件桥接到 UI 事件总线。

```python
from reuleauxcoder.interfaces.events import UIEventBus, AgentEventBridge

bus = UIEventBus()
bridge = AgentEventBridge(bus)

# 将 bridge 注册为 agent 事件处理器
agent.add_event_handler(bridge.on_agent_event)
```

桥接逻辑：

| Agent 事件 | UI 事件级别 |
|-----------|------------|
| `ERROR` | `ERROR` |
| `TOOL_CALL_START` | `DEBUG` |
| `TOOL_CALL_END` | `DEBUG` |
| 其他 | `INFO` |

## CLIRenderer

CLIRenderer 是 CLI 接口的渲染实现，订阅 UI 事件总线并渲染到终端。

### 处理 Agent 事件

```python
class CLIRenderer:
    def on_event(self, event: AgentEvent) -> None:
        """直接处理 Agent 事件（用于流式渲染）"""
        if event.event_type == AgentEventType.STREAM_TOKEN:
            self._render_token(event.data["token"])
        elif event.event_type == AgentEventType.TOOL_CALL_START:
            self._render_tool_start(event.tool_name, event.tool_args)
        # ...
```

### 处理 UI 事件

```python
class CLIRenderer:
    def on_ui_event(self, event: UIEvent) -> None:
        """处理 UI 总线事件"""
        if event.kind == UIEventKind.AGENT:
            # 桥接的 agent 事件，委托给 on_event
            agent_event = event.data.get("agent_event")
            if isinstance(agent_event, AgentEvent):
                self.on_event(agent_event)
            return
        
        # 渲染通用通知
        self._render_notification(event)
```

## 使用示例

### 在领域层发送事件

```python
# Agent 内部
self._emit_event(AgentEvent.stream_token(token))
self._emit_event(AgentEvent.tool_call_start(name, args))
```

### 在扩展层发送事件

```python
# MCP 扩展
if self._ui_bus:
    self._ui_bus.error("Connection failed", kind=UIEventKind.MCP)
    self._ui_bus.success("Connected with 5 tools", kind=UIEventKind.MCP)
```

### 在接口层发送事件

```python
# 命令处理
ui_bus.success(f"Session saved: {sid}", kind=UIEventKind.SESSION)
ui_bus.warning("Conversation reset.")

# REPL 循环
ui_bus.info("Bye!")
ui_bus.error(f"Error: {e}")
```

## 流式输出实现

流式输出通过 `on_token` 回调实现实时推送：

```python
# LLM 客户端
def chat(self, messages, tools, on_token: Callable[[str], None] = None):
    for chunk in stream:
        if delta.content:
            tokens.append(delta.content)
            if on_token is not None:
                on_token(delta.content)  # 实时回调

# Agent 循环
resp = self.agent.llm.chat(
    messages=self._full_messages(),
    tools=self._tool_schemas(),
    on_token=lambda token: self.agent._emit_event(AgentEvent.stream_token(token)),
)
```

## 扩展其他前端

要支持其他前端（如 Web API、TUI），只需：

1. 实现 `on_ui_event(event: UIEvent)` 方法
2. 订阅 UIEventBus
3. 根据事件级别和类型进行渲染

```python
class WebRenderer:
    def on_ui_event(self, event: UIEvent) -> None:
        if event.level == UIEventLevel.ERROR:
            self.websocket.send({"type": "error", "message": event.message})
        elif event.level == UIEventLevel.SUCCESS:
            self.websocket.send({"type": "success", "message": event.message})
        # ...
```

## 文件结构

```
reuleauxcoder/
├── domain/
│   └── agent/
│       └── events.py          # AgentEvent, AgentEventType
├── extensions/
│   └── mcp/
│       ├── client.py          # MCPClient (使用 ui_bus)
│       └── manager.py         # MCPManager (使用 ui_bus)
└── interfaces/
    ├── events.py              # UIEvent, UIEventBus, AgentEventBridge
    └── cli/
        ├── render.py          # CLIRenderer
        ├── main.py            # 创建 UIEventBus, 桥接
        ├── repl.py            # 使用 ui_bus
        └── commands.py        # 使用 ui_bus
```

## 设计原则

1. **领域层不依赖接口层**：Agent 只知道 `AgentEvent`，不知道 CLI
2. **扩展层可选依赖接口层**：MCP 扩展接收可选的 `ui_bus` 参数
3. **接口层统一渲染**：所有输出收敛到 `CLIRenderer`
4. **事件驱动流式**：通过回调实现真正的实时流式输出