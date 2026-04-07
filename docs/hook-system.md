# Hook 系统设计

## 概述

Hook 系统是内建的基础设施，允许插件在执行流程的特定点扩展和修改应用行为。Hook 本身不是扩展——它是插件注册逻辑的机制。

## 架构

```
核心层（内建）
├── Hook 系统       # 定义 hook 点，触发 hook
├── Plugin 系统     # 加载/管理插件
├── Mode 系统       # 模式状态管理
└── Tool Registry   # 工具注册表

插件层（扩展）
├── tools/          # 工具插件（通过 Plugin 接口注册工具）
├── hooks/          # Hook 插件（通过 Hook 系统注册逻辑）
│   ├── timestamp.py
│   ├── session_exit.py
│   └── permission.py
└── mcp/            # MCP 插件
```

## Hook 点定义

### 用户消息流程

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `before_user_message` | 用户消息发送前 | `str` 或 `dict` | 添加时间戳、注入上下文、过滤敏感信息 |
| `after_user_message` | 用户消息处理后 | `dict` | 日志记录、统计 |

### LLM 交互流程

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `before_llm_request` | 发送请求给 LLM 前 | `dict` | 修改参数、添加系统提示、token 估算 |
| `after_llm_response` | LLM 响应后 | `dict` | 记录 token 使用、响应分析 |
| `on_llm_error` | LLM 请求失败 | `Exception` | 错误处理、重试策略 |

### 工具执行流程

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `before_tool_execute` | 工具执行前 | `ToolCall` | 权限检查（Mode）、参数验证、审计日志 |
| `after_tool_execute` | 工具执行后 | `ToolResult` | 结果处理、日志、统计 |
| `on_tool_error` | 工具执行失败 | `Exception` | 错误处理、回滚 |

### 会话流程

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `before_session_save` | 会话保存前 | `list[dict]` | 添加退出标记、压缩历史 |
| `after_session_load` | 会话加载后 | `list[dict]` | 清理过期数据、格式转换 |
| `on_session_resume` | 恢复会话首条消息前 | `str` | 添加回来时间、恢复提示 |

### 模式流程

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `on_mode_switch` | 模式切换时 | `dict` | 通知用户、记录日志、限制切换条件 |

### MCP 流程

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `on_mcp_server_connect` | MCP server 连接时 | `str` | 日志、健康检查 |
| `on_mcp_server_disconnect` | MCP server 断开时 | `str` | 清理、通知 |
| `on_mcp_tool_added` | MCP 工具添加时 | `Tool` | 权限注册、日志 |

### 生命周期

| Hook 点 | 触发时机 | 数据类型 | 用途 |
|---------|----------|----------|------|
| `on_app_start` | 应用启动时 | `Config` | 初始化、加载配置 |
| `on_app_exit` | 应用退出时 | `None` | 清理、保存状态 |

## Hook 系统 API

### 核心实现

```python
# core/hooks.py
from typing import Any, Callable, Dict, List

# Hook 注册表 - 映射 hook 点到已注册的 hook 函数
HOOK_REGISTRY: Dict[str, List[Callable]] = {
    "before_user_message": [],
    "after_user_message": [],
    "before_llm_request": [],
    "after_llm_response": [],
    "on_llm_error": [],
    "before_tool_execute": [],
    "after_tool_execute": [],
    "on_tool_error": [],
    "before_session_save": [],
    "after_session_load": [],
    "on_session_resume": [],
    "on_mode_switch": [],
    "on_mcp_server_connect": [],
    "on_mcp_server_disconnect": [],
    "on_mcp_tool_added": [],
    "on_app_start": [],
    "on_app_exit": [],
}


def register_hook(point: str, hook: Callable) -> None:
    """为指定 hook 点注册 hook 函数。
    
    Args:
        point: hook 点名称
        hook: 接收 hook 数据并返回修改后数据的 callable
    """
    if point not in HOOK_REGISTRY:
        raise ValueError(f"未知的 hook 点: {point}")
    HOOK_REGISTRY[point].append(hook)


def unregister_hook(point: str, hook: Callable) -> None:
    """取消注册 hook 函数。"""
    if point in HOOK_REGISTRY:
        HOOK_REGISTRY[point] = [h for h in HOOK_REGISTRY[point] if h != hook]


def run_hooks(point: str, data: Any) -> Any:
    """运行指定 hook 点的所有 hook。
    
    Hook 按顺序执行，每个 hook 接收前一个 hook 的输出。
    
    Args:
        point: hook 点名称
        data: 传递给 hook 的初始数据
        
    Returns:
        所有 hook 处理后的修改数据
    """
    if point not in HOOK_REGISTRY:
        return data
    
    for hook in HOOK_REGISTRY[point]:
        try:
            data = hook(data)
        except Exception:
            # 不让 hook 错误中断执行
            pass
    
    return data


def clear_hooks(point: str = None) -> None:
    """清除指定 hook 点的所有 hook，或清除所有 hook。"""
    if point:
        HOOK_REGISTRY[point] = []
    else:
        for p in HOOK_REGISTRY:
            HOOK_REGISTRY[p] = []
```

### Hook 函数签名

Hook 函数应遵循以下签名：

```python
def hook_function(data: Any) -> Any:
    """处理 hook 数据并返回修改后的数据。
    
    Args:
        data: 传递给此 hook 点的数据（类型因 hook 点而异）
        
    Returns:
        修改后的数据（与输入类型相同，或适合该 hook 点的类型）
    """
    # 处理数据
    return modified_data
```

某些 hook 可能返回 `None` 表示不修改，或抛出异常来中止操作。

## Mode 系统

Mode 系统是内建功能，根据当前模式限制可用工具。它不是插件——是核心功能，插件可以通过 hook 与其交互。

模式定义从 `config.yaml` 读取，用户可以自定义模式。如果用户没有定义，则使用默认模式。

### 配置示例

```yaml
modes:
  # 用户自定义模式
  plan:
    name: "计划模式"
    description: "AI 只能读取和规划，不能执行写操作"
    allowed_tools: ["read_file", "glob", "grep", "list_directory", "get_file_info"]
  
  review:
    name: "审查模式"
    description: "AI 可以读取和建议修改，但不能执行"
    allowed_tools: ["read_file", "glob", "grep", "list_directory", "get_file_info"]
  
  full:
    name: "完全访问"
    description: "AI 拥有所有能力"
    allowed_tools: ["*"]  # 所有工具允许
  
  # 用户可以添加自定义模式
  readonly:
    name: "只读模式"
    description: "只能读取文件，不能搜索"
    allowed_tools: ["read_file", "get_file_info"]
  
  # 默认启动模式（可选）
  default: "full"
```

### 默认模式

如果用户没有在 config.yaml 中定义 modes，使用以下默认值：

```yaml
modes:
  plan:
    name: "计划模式"
    description: "AI 只能读取和规划，不能执行写操作"
    allowed_tools: ["read_file", "glob", "grep", "list_directory", "get_file_info"]
  
  full:
    name: "完全访问"
    description: "AI 拥有所有能力"
    allowed_tools: ["*"]
  
  default: "full"
```

### 模式定义

```python
# core/mode.py
from typing import Dict, List, Optional

# 默认模式（当用户没有定义时使用）
DEFAULT_MODES: Dict[str, Dict] = {
    "plan": {
        "name": "计划模式",
        "description": "AI 只能读取和规划，不能执行写操作",
        "allowed_tools": ["read_file", "glob", "grep", "list_directory", "get_file_info"],
    },
    "full": {
        "name": "完全访问",
        "description": "AI 拥有所有能力",
        "allowed_tools": ["*"],
    },
}

class ModeManager:
    """管理执行模式，限制工具可用性。"""
    
    def __init__(self, config_modes: Dict = None, default_mode: str = "full"):
        """初始化模式管理器。
        
        Args:
            config_modes: 从 config.yaml 读取的模式定义
            default_mode: 默认启动模式
        """
        # 使用用户定义的模式，或默认模式
        self.modes = config_modes or DEFAULT_MODES
        self.current_mode = default_mode
    
    def switch(self, mode: str) -> bool:
        """切换到不同模式。
        
        Returns:
            True 如果切换成功，False 如果模式不存在
        """
        if mode not in self.modes:
            return False
        
        old_mode = self.current_mode
        self.current_mode = mode
        
        # 触发模式切换 hook
        from core.hooks import run_hooks
        run_hooks("on_mode_switch", {"from": old_mode, "to": mode})
        
        return True
    
    def is_tool_allowed(self, tool_name: str) -> bool:
        """检查工具在当前模式下是否允许。"""
        allowed = self.modes[self.current_mode]["allowed_tools"]
        return allowed == ["*"] or tool_name in allowed
    
    def get_current_mode(self) -> str:
        """获取当前模式名称。"""
        return self.current_mode
    
    def get_mode_info(self, mode: str = None) -> Dict:
        """获取模式信息。"""
        mode = mode or self.current_mode
        return self.modes.get(mode, {})
    
    def list_modes(self) -> List[str]:
        """列出所有可用模式。"""
        return list(self.modes.keys())


# 全局实例（在加载配置后初始化）
mode_manager = None

def init_mode_manager(config_modes: Dict = None, default_mode: str = "full"):
    """初始化全局模式管理器。"""
    global mode_manager
    mode_manager = ModeManager(config_modes, default_mode)
```

### 模式 CLI 命令

```
/mode [name]     # 切换到指定模式
/mode            # 显示当前模式信息
/modes           # 列出所有可用模式
```

## 内置 Hook 插件

### 时间戳 Hook

为用户消息添加时间戳。

```python
# plugins/hooks/timestamp.py
import time
from core.hooks import register_hook

def add_timestamp_to_message(message: dict) -> dict:
    """为消息添加时间戳。"""
    message["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return message

def register():
    register_hook("before_user_message", add_timestamp_to_message)
```

### 会话退出 Hook

退出保存时添加退出标记。

```python
# plugins/hooks/session_exit.py
import time
from core.hooks import register_hook

def add_exit_marker(messages: list) -> list:
    """保存前添加退出标记。"""
    exit_time = time.strftime("%Y-%m-%d %H:%M:%S")
    messages.append({
        "role": "system",
        "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
    })
    return messages

def register():
    register_hook("before_session_save", add_exit_marker)
```

### 会话恢复 Hook

恢复会话时添加回来时间上下文。

```python
# plugins/hooks/session_resume.py
import time
from core.hooks import register_hook

def add_resume_context(user_input: str) -> str:
    """为恢复后的首条消息添加上下文。"""
    # 实际实现需要访问会话退出时间
    # 这在 REPL 层处理
    return user_input

def register():
    register_hook("on_session_resume", add_resume_context)
```

### 权限 Hook

根据当前模式检查工具权限。

```python
# plugins/hooks/permission.py
from core.hooks import register_hook
from core.mode import mode_manager

def check_tool_permission(tool_call) -> dict:
    """检查工具在当前模式下是否允许。
    
    Returns:
        如果允许返回 tool_call，否则返回 blocked 结果
    """
    if not mode_manager.is_tool_allowed(tool_call.name):
        return {
            "blocked": True,
            "tool": tool_call.name,
            "reason": f"工具 '{tool_call.name}' 在 {mode_manager.current_mode} 模式下不允许",
            "suggestion": f"使用 /mode full 切换到完全访问模式",
        }
    return tool_call

def register():
    register_hook("before_tool_execute", check_tool_permission)
```

## 插件配置

通过 `config.yaml` 启用/禁用和配置插件：

```yaml
plugins:
  # 启用/禁用插件
  enabled:
    - hooks.timestamp
    - hooks.session_exit
    - hooks.session_resume
    - hooks.permission
    - tools.bash
    - tools.read
    - tools.write
  
  # 插件特定配置
  config:
    hooks.timestamp:
      format: "%Y-%m-%d %H:%M:%S"
    
    hooks.session_exit:
      enabled: true
    
    hooks.permission:
      strict: false  # 如果 true，被阻止的工具抛出错误而不是返回消息
```

## 集成点

### Agent 集成

```python
# domain/agent/agent.py
from core.hooks import run_hooks

class Agent:
    def chat(self, user_input: str) -> str:
        # 运行 before_user_message hooks
        user_input = run_hooks("before_user_message", user_input)
        
        self.state.messages.append({"role": "user", "content": user_input})
        ...
```

### 工具执行集成

```python
# domain/agent/tool_execution.py
from core.hooks import run_hooks

class ToolExecutor:
    def execute(self, tool_call) -> str:
        # 运行 before_tool_execute hooks
        result = run_hooks("before_tool_execute", tool_call)
        
        # 检查工具是否被权限 hook 阻止
        if isinstance(result, dict) and result.get("blocked"):
            return f"工具被阻止: {result['reason']}"
        
        # 执行工具
        tool_output = tool.execute(**tool_call.arguments)
        
        # 运行 after_tool_execute hooks
        run_hooks("after_tool_execute", {"tool": tool_call.name, "result": tool_output})
        
        return tool_output
```

### 会话集成

```python
# services/sessions/manager.py
from core.hooks import run_hooks

def save_session(messages: list, ...) -> str:
    # 运行 before_session_save hooks
    messages = run_hooks("before_session_save", messages)
    
    # 保存到磁盘
    ...

def load_session(session_id: str, ...) -> tuple:
    messages, model = ...
    
    # 运行 after_session_load hooks
    messages = run_hooks("after_session_load", messages)
    
    return messages, model
```

### REPL 集成

```python
# interfaces/cli/repl.py
from core.hooks import run_hooks

def run_repl(...):
    first_message_after_resume = session_exit_time is not None
    
    while True:
        user_input = prompt("You > ")
        
        if first_message_after_resume:
            # 运行 on_session_resume hooks
            user_input = run_hooks("on_session_resume", user_input)
            first_message_after_resume = False
        
        ...
```

## 未来考虑

### Hook 优先级

Hook 可能需要执行顺序控制：

```python
register_hook("before_tool_execute", check_permission, priority=100)  # 高优先级
register_hook("before_tool_execute", log_tool_call, priority=50)      # 低优先级
```

### Hook 链

Hook 可以条件执行链式调用：

```python
# 只有前一个返回 True 才执行下一个
def conditional_hook_chain(point: str, data: Any) -> Any:
    for hook in HOOK_REGISTRY[point]:
        result = hook(data)
        if result is False:  # 中止链
            return data
        data = result
    return data
```

### 异步 Hook

用于异步操作：

```python
async def run_async_hooks(point: str, data: Any) -> Any:
    for hook in HOOK_REGISTRY[point]:
        if asyncio.iscoroutinefunction(hook):
            data = await hook(data)
        else:
            data = hook(data)
    return data
```

### Hook 元数据

Hook 可以提供调试用的元数据：

```python
@hook_metadata(name="permission_check", version="1.0", author="system")
def check_tool_permission(tool_call):
    ...
```