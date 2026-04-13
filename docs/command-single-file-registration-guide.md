# 单文件自包含注册 Command 指南

本文介绍 ReuleauxCoder 的命令注册机制，以及如何在**单个文件**内完成命令的定义、解析、处理与注册。

---

## 1. 注册机制概述

### 1.1 设计意图

ReuleauxCoder 的扩展系统采用分层设计：

```
reuleauxcoder/extensions/
├── command/
│   ├── builtin/         # 内置命令（随应用打包，当前已实现）
│   └── [external/]      # 外部插件（未来预留）
├── tools/
│   ├── builtin/         # 内置工具
│   └── [external/]      # 外部插件（未来预留）
├── mcp/                 # MCP 服务器扩展
└── [plugins/]           # 用户插件目录（未来预留）
```

**当前状态**：loader 只扫描 `builtin/` 目录，这是"内置扩展"层。

**未来扩展**：设计预留了插件系统扩展点：
- 可添加 `external/` 目录支持第三方扩展
- 可添加用户插件目录（如 `~/.config/rcoder/plugins/`）
- `@register_command_module` 装饰器机制可复用于插件加载

### 1.2 核心组件

| 文件 | 职责 |
|------|------|
| `reuleauxcoder/app/commands/loader.py` | 扫描并加载命令模块（当前仅 builtin） |
| `reuleauxcoder/app/commands/module_registry.py` | 管理命令模块注册表（装饰器实现） |
| `reuleauxcoder/app/commands/registry.py` | ActionSpec 的存储与查找 |
| `reuleauxcoder/app/commands/specs.py` | ActionSpec、TriggerSpec 等规格定义 |
| `reuleauxcoder/app/commands/models.py` | CommandContext、CommandResult 等运行时模型 |

### 1.3 加载流程

```
启动时
    │
    ▼
loader._import_builtin_command_modules()
    │  扫描 reuleauxcoder/extensions/command/builtin/*.py
    │  逐个 import（触发模块级装饰器）
    ▼
@register_command_module 装饰器执行
    │  将 register_actions 函数加入 _REGISTRARS 列表
    ▼
create_builtin_action_registry()
    │  创建 ActionRegistry 实例
    │  遍历 _REGISTRARS，调用每个 register_actions(registry)
    ▼
ActionRegistry.register(ActionSpec)
    │  将 ActionSpec 存入内部列表
    ▼
运行时：用户输入 → registry.parse() → 匹配 parser → 调用 handler
```

### 1.4 关键约定

**当前限制**：
- 命令模块必须放在 `reuleauxcoder/extensions/command/builtin/` 目录
- loader 使用 `_BUILTIN_COMMAND_PACKAGE` 常量硬编码扫描路径
- 文件名不能以 `_` 开头（会被 loader 跳过）

**为什么限制在 builtin**：
- `builtin` = "内置扩展"，随应用源码打包发布
- 与未来插件系统隔离：插件可能来自外部包或用户目录
- 保证核心命令的稳定性和版本一致性

**未来插件支持**（设计预留）：
```python
# loader.py 可能的扩展方式
def create_action_registry(include_plugins: bool = True) -> ActionRegistry:
    registry = ActionRegistry()
    _import_builtin_command_modules()  # 内置命令
    if include_plugins:
        _import_plugin_command_modules()  # 用户插件
    for register in iter_command_module_registrars():
        register(registry)
    return registry
```

---

## 2. 单文件命令模板

### 2.1 文件结构

文件位置：`reuleauxcoder/extensions/command/builtin/<feature>.py`

一个完整的命令模块包含以下部分：

```
┌─────────────────────────────────────────────────────────────┐
│  1. 导入依赖                                                 │
├─────────────────────────────────────────────────────────────┤
│  2. Command 数据类（定义命令参数）                            │
├─────────────────────────────────────────────────────────────┤
│  3. Parser 函数（解析用户输入 → 返回 Command 对象或 None）     │
├─────────────────────────────────────────────────────────────┤
│  4. Handler 函数（执行业务逻辑 → 返回 CommandResult）         │
├─────────────────────────────────────────────────────────────┤
│  5. register_actions（带 @register_command_module 装饰器）   │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 最小示例

```python
from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import TEXT_REQUIRED, UI_TARGETS, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec


# ─────────────────────────────────────────────────────────────
# 1. Command 数据类
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class MyCommand:
    name: str


# ─────────────────────────────────────────────────────────────
# 2. Parser 函数
# ─────────────────────────────────────────────────────────────
def _parse_my_command(user_input: str, parse_ctx):
    """解析用户输入，返回 Command 对象或 None。"""
    captures = match_template(user_input, "/mycmd {name}")
    if captures is None:
        return None
    return MyCommand(name=captures["name"])


# ─────────────────────────────────────────────────────────────
# 3. Handler 函数
# ─────────────────────────────────────────────────────────────
def _handle_my_command(command, ctx) -> CommandResult:
    """执行命令逻辑，通过 ctx.ui_bus 反馈结果。"""
    ctx.ui_bus.success(f"hello {command.name}")
    return CommandResult(action="continue")


# ─────────────────────────────────────────────────────────────
# 4. 注册函数
# ─────────────────────────────────────────────────────────────
@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register(
        ActionSpec(
            action_id="myfeature.run",      # 唯一标识，格式: <feature>.<action>
            feature_id="myfeature",          # 功能分组
            description="Run my feature",    # 命令描述（用于 help）
            ui_targets=UI_TARGETS,           # 支持的 UI 目标
            required_capabilities=TEXT_REQUIRED,  # 所需能力
            triggers=(slash_trigger("/mycmd <name>"),),  # 触发方式
            parser=_parse_my_command,
            handler=_handle_my_command,
        )
    )
```

### 2.3 无参数命令示例

对于不需要参数的命令，使用 `EmptyCommand`：

```python
from reuleauxcoder.app.commands.shared import EmptyCommand

def _parse_help(user_input: str, parse_ctx):
    if match_template(user_input, "/help") is not None:
        return EmptyCommand()
    return None

def _handle_help(command, ctx) -> CommandResult:
    ctx.ui_bus.info("Showing help...")
    return CommandResult(action="continue")
```

### 2.4 多别名命令示例

使用 `matches_any` 支持多个触发词：

```python
from reuleauxcoder.app.commands.matchers import matches_any

def _parse_exit(user_input: str, parse_ctx):
    if matches_any(user_input, ("/quit", "/exit"), case_insensitive=True):
        return ExitCommand()
    return None
```

### 2.5 带参数验证的命令示例

```python
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.shared import non_empty_text, enum_text

_VALID_STRATEGIES = {"snip", "summarize", "collapse"}

def _parse_compact(user_input: str, parse_ctx):
    # 无参数形式
    if match_template(user_input, "/compact") is not None:
        return CompactCommand(strategy=None)

    # 带参数形式
    captures = match_template(user_input, "/compact {strategy}", case_insensitive=True)
    if captures is None:
        return None

    try:
        strategy = enum_text(_VALID_STRATEGIES).parse(captures["strategy"])
    except ParamParseError:
        # 参数非法时返回带空值的 Command，由 handler 报错
        return CompactCommand(strategy="")

    return CompactCommand(strategy=strategy)
```

---

## 3. 可用工具参考

### 3.1 输入匹配（`matchers.py`）

| 函数 | 签名 | 说明 |
|------|------|------|
| `normalize_input` | `(user_input: str) -> str` | 规范化空白字符，返回单空格分隔的字符串 |
| `match_template` | `(user_input, template, *, case_insensitive=False) -> dict \| None` | 模板匹配，成功返回捕获字典，失败返回 `None` |
| `matches_any` | `(user_input, templates, *, case_insensitive=False) -> bool` | 检查是否匹配任意模板 |

**模板占位符**：

| 占位符 | 说明 | 示例 |
|--------|------|------|
| `{name}` | 匹配单个 token（非空白字符序列） | `/model {profile}` 匹配 `/model gpt4` |
| `{name+}` | 匹配剩余所有内容（必须放在模板末尾） | `/session {target+}` 匹配 `/session some id with spaces` |

**示例**：

```python
from reuleauxcoder.app.commands.matchers import match_template, matches_any

# 基本匹配
captures = match_template(user_input, "/model {profile}")
if captures:
    profile_name = captures["profile"]  # 单个 token

# 捕获剩余内容
captures = match_template(user_input, "/session {target+}")
if captures:
    target = captures["target"]  # 可能包含空格

# 多个别名
if matches_any(user_input, ("/quit", "/exit", "/bye"), case_insensitive=True):
    return ExitCommand()

# 大小写不敏感
captures = match_template(user_input, "/compact {strategy}", case_insensitive=True)
```

### 3.2 参数解析（`params.py`）

**解析器类**：

| 类 | 构造参数 | `parse()` 返回类型 | 说明 |
|----|----------|-------------------|------|
| `StrParam` | `strip=True, lower=False, non_empty=False, reject=frozenset()` | `str` | 字符串解析 |
| `EnumParam` | `values: frozenset, case_insensitive=False` | `str` | 枚举值验证 |
| `BoolParam` | `true_values, false_values, case_insensitive=True` | `bool` | 布尔解析 |
| `IntParam` | `min_value=None, max_value=None` | `int` | 整数解析，支持范围 |
| `FloatParam` | `min_value=None, max_value=None` | `float` | 浮点数解析 |
| `ChoiceParam` | `choices: Mapping[str, Any], case_insensitive=True` | `Any` | 选项映射到任意值 |

**异常**：

- `ParamParseError`：参数解析失败时抛出，继承自 `ValueError`

**示例**：

```python
from reuleauxcoder.app.commands.params import StrParam, IntParam, EnumParam, ParamParseError

# 非空字符串，排除特定值
profile_parser = StrParam(non_empty=True, reject=frozenset({"ls", "list", "show"}))
try:
    profile = profile_parser.parse(captures["profile"])
except ParamParseError:
    return None

# 整数范围
limit_parser = IntParam(min_value=1, max_value=100)
limit = limit_parser.parse(captures["limit"])

# 枚举
strategy_parser = EnumParam(values=frozenset({"snip", "summarize", "collapse"}), case_insensitive=True)
strategy = strategy_parser.parse(captures["strategy"])
```

**批量解析**：

```python
from reuleauxcoder.app.commands.params import parse_captures, StrParam, IntParam

schema = {
    "name": StrParam(non_empty=True),
    "count": IntParam(min_value=1),
}
parsed = parse_captures(captures, schema)
if parsed is None:
    return None
# parsed = {"name": "...", "count": 5}
```

### 3.3 共用工具（`shared.py`）

**常量**：

| 常量 | 值 | 说明 |
|------|-----|------|
| `UI_TARGETS` | `frozenset({"cli", "tui", "vscode"})` | 默认支持的 UI 目标 |
| `TEXT_REQUIRED` | `frozenset({UICapability.TEXT_INPUT})` | 默认能力要求 |

**类和函数**：

| 名称 | 签名 | 说明 |
|------|------|------|
| `EmptyCommand` | `@dataclass` | 无参数命令的占位对象，无需任何字段 |
| `slash_trigger` | `(value: str) -> TriggerSpec` | 快速创建 slash 类型触发器 |
| `non_empty_text` | `(*, lower=False, reject=frozenset()) -> StrParam` | 非空字符串解析器 |
| `enum_text` | `(values: set \| frozenset, *, case_insensitive=True) -> EnumParam` | 枚举字符串解析器 |

**示例**：

```python
from reuleauxcoder.app.commands.shared import (
    EmptyCommand, UI_TARGETS, TEXT_REQUIRED,
    slash_trigger, non_empty_text, enum_text
)

# 无参数命令
def _parse_help(user_input, parse_ctx):
    if match_template(user_input, "/help") is not None:
        return EmptyCommand()
    return None

# 注册时使用 slash_trigger
ActionSpec(
    triggers=(slash_trigger("/help"),),
    # ...
)

# 使用 non_empty_text
name = non_empty_text(lower=True, reject=frozenset({"admin", "root"})).parse(captures["name"])

# 使用 enum_text
strategy = enum_text({"snip", "summarize", "collapse"}).parse(captures["strategy"])
```

### 3.4 规格声明（`specs.py`）

**TriggerKind 枚举**：

| 值 | 说明 |
|----|------|
| `SLASH` | 斜杠命令（如 `/help`） |
| `PALETTE` | 命令面板 |
| `BUTTON` | 按钮触发 |
| `MENU` | 菜单项 |
| `SHORTCUT` | 快捷键 |

**TriggerSpec**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | `TriggerKind` | 触发器类型 |
| `value` | `str` | 触发值（如命令字符串） |
| `ui_targets` | `frozenset[str]` | 目标 UI |
| `required_capabilities` | `frozenset[UICapability]` | 所需能力 |

**ActionSpec**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | `str` | 唯一标识，建议格式 `<feature>.<action>` |
| `feature_id` | `str` | 功能分组标识 |
| `description` | `str` | 命令描述，用于帮助显示 |
| `ui_targets` | `frozenset[str]` | 支持的 UI 目标 |
| `required_capabilities` | `frozenset[UICapability]` | 所需 UI 能力 |
| `triggers` | `tuple[TriggerSpec, ...]` | 触发方式列表 |
| `parser` | `Callable[[str, CommandParseContext], object \| None]` | 解析函数 |
| `handler` | `Callable[[object, CommandContext], CommandResult]` | 处理函数 |
| `interactive` | `bool` | 是否为交互式命令 |

**CommandParseContext**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `current_session_id` | `str \| None` | 当前会话 ID |
| `ui_profile` | `UIProfile` | 当前 UI 配置 |

### 3.5 上下文与返回值（`models.py`）

**CommandContext**（handler 第二个参数）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent` | `Any` | Agent 实例，包含 `messages`、`state`、`llm`、`context` 等 |
| `config` | `Any` | 配置对象，包含 `model`、`api_key`、`model_profiles` 等 |
| `ui_bus` | `Any` | UI 事件总线，用于发送消息、打开视图等 |
| `ui_profile` | `UIProfile \| None` | 当前 UI 配置信息 |
| `action_registry` | `ActionRegistry \| None` | 命令注册表（用于 help 命令） |
| `ui_interactor` | `UIInteractor \| None` | UI 交互器 |
| `sessions_dir` | `Path \| None` | 会话存储目录 |

**ui_bus 常用方法**：

```python
ctx.ui_bus.success(message, kind=UIEventKind.MODEL, **kwargs)  # 成功消息
ctx.ui_bus.error(message, kind=UIEventKind.SESSION, **kwargs)  # 错误消息
ctx.ui_bus.info(message, **kwargs)      # 信息消息
ctx.ui_bus.warning(message, **kwargs)   # 警告消息
ctx.ui_bus.open_view(view_type, title, payload, reuse_key)  # 打开视图
ctx.ui_bus.refresh_view(view_type, title, payload, reuse_key)  # 刷新视图
```

**CommandResult**（handler 返回值）：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `action` | `"continue" \| "chat" \| "exit"` | `"continue"` | 后续动作 |
| `session_id` | `str \| None` | `None` | 会话 ID（用于恢复/切换会话） |
| `session_exit_time` | `str \| None` | `None` | 会话退出时间 |
| `notifications` | `list[UIEvent]` | `[]` | 通知列表 |
| `view_requests` | `list[OpenViewRequest]` | `[]` | 视图请求列表 |
| `payload` | `dict[str, Any]` | `{}` | 附加数据 |

**OpenViewRequest**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `view_type` | `str` | 视图类型标识 |
| `title` | `str` | 视图标题 |
| `payload` | `dict[str, Any]` | 视图数据 |
| `focus` | `bool` | 是否聚焦（默认 `True`） |
| `reuse_key` | `str \| None` | 复用键（相同键的视图会复用） |

**示例**：

```python
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest

def _handle_my_command(command, ctx) -> CommandResult:
    # 发送成功消息
    ctx.ui_bus.success(f"Processed: {command.name}")

    # 打开视图
    payload = {"items": [...], "markdown": "..."}
    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="my_view",
                title="My View",
                payload=payload,
                reuse_key="my_view",
            )
        ],
        payload=payload,
    )
```

---

## 4. 编写规范

### 4.1 文件组织

```
reuleauxcoder/extensions/command/builtin/
├── __init__.py          # 包初始化（可为空）
├── model.py             # 模型相关命令
├── sessions.py          # 会话相关命令
├── approval.py          # 审批规则命令
├── system.py            # 系统命令（help, exit, reset 等）
└── mcp.py               # MCP 服务器命令
```

**命名约定**：
- 文件名：`<feature>.py`，对应 `feature_id`
- Command 类：`<Action>Command`（如 `SwitchModelCommand`、`ResumeSessionCommand`）
- Parser 函数：`_parse_<action>`（私有，前缀下划线）
- Handler 函数：`_handle_<action>`（私有，前缀下划线）

### 4.2 Parser 编写规范

**职责**：
- 识别用户输入是否匹配命令
- 提取参数并构造 Command 对象
- 不执行业务逻辑

**返回值约定**：

| 情况 | 返回值 |
|------|--------|
| 输入不匹配 | `None` |
| 输入匹配，参数有效 | 填充参数的 Command 对象 |
| 输入匹配，参数无效 | 带空值/标记的 Command 对象（由 handler 报错） |

**示例**：

```python
def _parse_switch_model(user_input: str, parse_ctx):
    # 情况1：不匹配
    captures = match_template(user_input, "/model {profile+}")
    if captures is None:
        return None

    # 情况3：参数无效（返回带空值的 Command，让 handler 报错）
    try:
        profile = non_empty_text(reject=frozenset({"ls", "list", "show"})).parse(captures["profile"])
    except ParamParseError:
        return SwitchModelCommand(profile_name="")  # 空值标记

    # 情况2：参数有效
    return SwitchModelCommand(profile_name=profile)
```

### 4.3 Handler 编写规范

**职责**：
- 执行业务逻辑
- 通过 `ctx.ui_bus` 发送 UI 反馈
- 返回 `CommandResult`

**常用模式**：

```python
def _handle_switch_model(command, ctx) -> CommandResult:
    # 1. 参数验证（处理 parser 传来的无效参数）
    if not command.profile_name:
        ctx.ui_bus.error("Profile name is required.")
        return CommandResult(action="continue")

    # 2. 业务逻辑
    profile = ctx.config.model_profiles.get(command.profile_name)
    if profile is None:
        ctx.ui_bus.error(f"Unknown profile: {command.profile_name}")
        return CommandResult(action="continue")

    # 3. 执行操作
    ctx.agent.llm.reconfigure(model=profile.model, ...)
    ctx.ui_bus.success(f"Switched to {command.profile_name}")

    # 4. 返回结果
    return CommandResult(action="continue", payload={"profile": command.profile_name})
```

### 4.4 注册函数规范

**一个模块注册多个命令**：

```python
@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    # 使用 register_many 批量注册
    registry.register_many([
        ActionSpec(
            action_id="model.show",
            feature_id="model",
            description="Show model profiles",
            ui_targets=UI_TARGETS,
            required_capabilities=TEXT_REQUIRED,
            triggers=(slash_trigger("/model"),),
            parser=_parse_show_model,
            handler=_handle_show_model,
        ),
        ActionSpec(
            action_id="model.switch",
            feature_id="model",
            description="Switch active model profile",
            ui_targets=UI_TARGETS,
            required_capabilities=TEXT_REQUIRED,
            triggers=(slash_trigger("/model <profile>"),),
            parser=_parse_switch_model,
            handler=_handle_switch_model,
        ),
    ])
```

**action_id 命名规范**：
- 格式：`<feature>.<action>`
- 示例：`model.show`、`model.switch`、`sessions.list`、`sessions.resume`

### 4.5 最佳实践

1. **单一职责**：一个模块对应一个功能领域
2. **Parser 与 Handler 分离**：Parser 只解析，Handler 只执行
3. **使用 dataclass**：Command 类使用 `@dataclass(frozen=True, slots=True)`
4. **私有函数**：Parser 和 Handler 使用下划线前缀
5. **类型注解**：Handler 返回值标注 `-> CommandResult`
6. **错误处理**：通过 `ctx.ui_bus.error()` 反馈错误，返回 `CommandResult(action="continue")`
7. **视图复用**：使用 `reuse_key` 避免重复打开视图

---

## 5. 自检清单

### 5.1 文件检查

- [ ] 文件位于 `reuleauxcoder/extensions/command/builtin/` 目录
- [ ] 文件名不以 `_` 开头（否则会被 loader 跳过）
- [ ] 文件包含必要的导入语句

### 5.2 注册检查

- [ ] `register_actions` 函数带有 `@register_command_module` 装饰器
- [ ] `ActionSpec` 的 `action_id` 唯一（不与其他模块冲突）
- [ ] `ActionSpec` 的 `feature_id` 与文件名对应
- [ ] `triggers` 使用 `slash_trigger()` 或正确的 `TriggerSpec`

### 5.3 Parser/Handler 检查

- [ ] Parser 函数签名：`(user_input: str, parse_ctx) -> Command | None`
- [ ] Handler 函数签名：`(command, ctx) -> CommandResult`
- [ ] Parser 返回 `None` 表示不匹配
- [ ] Handler 通过 `ctx.ui_bus` 反馈结果

### 5.4 编译与运行检查

- [ ] `python -m compileall reuleauxcoder/extensions/command/builtin/` 编译通过
- [ ] 启动应用后 `/help` 能显示新命令
- [ ] 命令能正确解析并执行

### 5.5 测试命令

```bash
# 编译检查
python -m compileall reuleauxcoder/extensions/command/builtin/

# 启动应用测试
rcoder

# 在应用内测试
/help          # 查看命令列表
/<yourcmd>     # 测试命令
```

---

## 6. 现有命令参考

| 模块 | 命令 | 说明 |
|------|------|------|
| `model.py` | `/model` | 显示模型配置 |
| `model.py` | `/model <profile>` | 切换模型配置 |
| `sessions.py` | `/sessions` | 列出保存的会话 |
| `sessions.py` | `/session <id>` | 恢复会话 |
| `sessions.py` | `/save` | 保存当前会话 |
| `sessions.py` | `/new` | 开始新会话 |
| `approval.py` | `/approval` | 显示审批规则 |
| `approval.py` | `/approval set <target> <action>` | 设置审批规则 |
| `system.py` | `/help` | 显示帮助 |
| `system.py` | `/quit` `/exit` | 退出应用 |
| `system.py` | `/reset` | 重置对话 |
| `system.py` | `/compact` | 压缩上下文 |
| `system.py` | `/tokens` | 显示 token 使用情况 |
| `mcp.py` | `/mcp` | 显示 MCP 服务器 |
| `mcp.py` | `/mcp enable <server>` | 启用 MCP 服务器 |
| `mcp.py` | `/mcp disable <server>` | 禁用 MCP 服务器 |