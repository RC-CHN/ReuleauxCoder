# ReuleauxCoder Project Context

## Architecture Overview

This project follows a layered architecture with clear separation of concerns:

```
reuleauxcoder/
├── domain/          # Core domain logic (pure, no external dependencies)
├── services/        # Service layer (coordinates domain objects)
├── infrastructure/  # Infrastructure (external resource access)
├── interfaces/      # User interface layer (CLI, TUI)
├── extensions/      # Extension system (pluggable features)
├── app/             # Application layer (use case orchestration)
└── compat/          # Compatibility handling
```

## Layer Responsibilities

### Domain Layer (`domain/`)
Pure business logic with no external dependencies. Contains core abstractions and types.

- **agent/**: Agent core (`Agent`, `AgentLoop`, `AgentState`, `ToolExecutor`)
- **config/**: Configuration domain models (`Config`, `ModeConfig`, `ApprovalConfig`)
- **context/**: Context management (token counting, compression, wall hits)
- **hooks/**: Hook system (`HookRegistry`, `HookBase`, `GuardHook`, `TransformHook`, `ObserverHook`)
- **llm/**: LLM domain models (`LLMResponse`, `ToolCall`)
- **session/**: Session domain types

### Services Layer (`services/`)
Coordinates domain objects and handles external interactions.

- **llm/**: LLM client (OpenAI API, streaming, retry, diagnostics)
- **prompt/**: System prompt builder (`PromptAssembler`, `PromptBlock`, `PromptZone`)
- **config/**: Configuration loading, validation, defaults

### Infrastructure Layer (`infrastructure/`)
External resource access and low-level utilities.

- **fs/**: File system paths and utilities
- **persistence/**: Storage (session store, config store, skills config store)
- **yaml/**: YAML loading utilities
- **openai/**: OpenAI SDK wrapper

### Interfaces Layer (`interfaces/`)
User-facing interfaces.

- **cli/**: CLI REPL, commands, rendering, views
- **tui/**: TUI interface (in development)
- **entrypoint/**: Application runner (`AppRunner`, `AppContext`, `AppDependencies`)

### Extensions Layer (`extensions/`)
Pluggable extension system.

- **tools/**: Built-in tools (`shell`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `agent`)
- **command/**: Slash commands (`/model`, `/skills`, `/mcp`, `/sessions`, `/mode`, `/approval`)
- **mcp/**: MCP server integration (`MCPManager`, `MCPClient`, `MCPTool`)
- **skills/**: Skills system (`SkillsService`, discovery, catalog)
- **subagent/**: Sub-agent management (`SubagentManager`, jobs, approval)

## Key Components

### Agent (`domain/agent/agent.py`)
The main orchestrator that coordinates LLM and tools.
- Manages conversation state (`messages`, `tokens`, `rounds`)
- Registers and executes hooks
- Handles mode switching

### AgentLoop (`domain/agent/loop.py`)
Manages the conversation loop.
- Builds system prompt via `system_prompt()`
- Injects runtime context at message tail
- Handles tool call execution flow

### ContextManager (`domain/context/manager.py`)
Manages conversation context and token limits.
- Token counting with tiktoken (o200k_base encoding)
- Context compression when approaching limits
- Wall hit detection for context boundaries

### Hook System (`domain/hooks/`)
Intercept execution at defined points.

Hook points:
- **LLM/Tools**: `BEFORE_TOOL_EXECUTE`, `AFTER_TOOL_EXECUTE`, `BEFORE_LLM_REQUEST`, `AFTER_LLM_RESPONSE`
- **Lifecycle**: `RUNNER_STARTUP`, `RUNNER_SHUTDOWN`, `SESSION_START`, `SESSION_SAVE`

Hook types:
- `GuardHook`: Decide whether execution may continue (returns `GuardDecision`)
- `TransformHook`: Modify context data (returns same-type context)
- `ObserverHook`: Inspect execution without mutation (returns None)

Built-in hooks:
- `ToolPolicyGuardHook` (`BEFORE_TOOL_EXECUTE`): Evaluate tool policies before execution
- `ToolOutputTruncationHook` (`AFTER_TOOL_EXECUTE`): Truncate and archive oversized output
- `ProjectContextHook` (`BEFORE_LLM_REQUEST`): Inject AGENT.md and similar files into messages
- `ProjectContextStartupNotifier` (`RUNNER_STARTUP`): Notify UI when project context files are found

Use `@register_hook(HookPoint, priority)` decorator for self-contained hook definitions.

### Prompt Builder (`services/prompt/builder.py`)
Constructs system prompt with cache-friendly ordering.

Zones (by cache stability):
- `STATIC`: Nearly constant content (identity, tools, rules)
- `SEMI_STATIC`: May change within session (skills, modes, user instructions)

Blocks are sorted by `(zone, order, key)` for deterministic output.

### AppRunner (`interfaces/entrypoint/runner.py`)
Application initialization and dependency injection.
- Uses `AppDependencies` for customizable component construction
- Auto-discovers hooks via `discover_hook_specs()` + `instantiate_hooks()`
- Manages MCP servers, skills service, session store

## Configuration (`config.yaml`)

Key sections:
- `models.profiles`: Model profiles with API keys, base URLs, token limits
- `modes`: Agent modes with tool restrictions and prompt append
- `approval`: Tool approval rules (allow, warn, require_approval, deny)
- `prompt.system_append`: Custom system prompt injection
- `skills`: Skills discovery and enable/disable
- `mcp.servers`: MCP server configurations

## Coding Conventions

1. **Layer isolation**: Domain layer must not import from services/infrastructure/interfaces
2. **TYPE_CHECKING imports**: Use `if TYPE_CHECKING:` for forward references
3. **Dataclasses with slots**: Prefer `@dataclass(slots=True)` for performance
4. **Explicit types**: Avoid `Any` when possible; use specific types
5. **Factory methods**: Use `create_from_config(cls, config)` for hook instantiation
6. **Deterministic ordering**: Sort collections before passing to prompt builder

## Extension Guidelines

### Adding a new tool
1. Create class inheriting from `Tool` base in `extensions/tools/builtin/`
2. Implement `name`, `description`, `schema()`, `execute()`
3. Add to `ALL_TOOLS` list in `extensions/tools/registry.py`

### Adding a new slash command
1. Create handler in `extensions/command/builtin/`
2. Register in command dispatcher

### Adding a new hook
1. Create class inheriting from `GuardHook`, `TransformHook`, or `ObserverHook`
2. Add `@register_hook(HookPoint, priority)` decorator
3. Implement `create_from_config(cls, config)` classmethod
4. Implement `run(self, context)` method
5. Import in `domain/hooks/builtin/__init__.py` and `discovery.py`

## Testing

Tests are in `tests/` mirroring the source structure.
Run with: `uv run python -m pytest tests/ -v`