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

reuleauxcoder-agent/  # Go binary for remote peer execution
├── cmd/reuleauxcoder-agent/main.go    # CLI entrypoint
├── internal/
│   ├── client/http.go                 # HTTP client for relay protocol
│   ├── runner/runner.go               # Main loop (register/heartbeat/poll/execute)
│   ├── protocol/types.go              # Protocol type definitions
│   └── tools/execute.go               # Local tool execution (shell/read/write/edit/glob/grep)
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
  - `Tool` base class with `name`, `description`, `parameters`, `execute()`, `preflight_validate()`
  - `@backend_handler("backend_id")` decorator for local/remote dispatch
  - `ExecutionContext` carries `peer_id`, `cwd`, `workspace_root`, `remote_stream_handler`
  - `ShellDangerousCommandPolicy` blocks rm -rf, fork bombs, curl|bash, etc.
- **command/**: Slash commands (`/model`, `/skills`, `/mcp`, `/sessions`, `/mode`, `/approval`)
- **mcp/**: MCP server integration (`MCPManager`, `MCPClient`, `MCPTool`)
  - `MCPManager` runs independent asyncio event loop in background thread
  - `MCPClient` is stdio JSON-RPC client with reconnect-once behavior
  - Remote tools wrapped as `MCPTool` with schema translation
- **skills/**: Skills system (`SkillsService`, discovery, catalog)
- **subagent/**: Sub-agent management (`SubagentManager`, jobs, approval)
  - `ThreadPoolExecutor(max_workers=4)` caps parallel explore jobs
  - `SubagentJob` tracks status, timing, result/error
  - `DelegatingSubagentApprovalProvider` uses parent LLM as secondary judge

## Current Runtime Architecture (Phase 1)

The project now treats a saved session as a **runtime overlay on top of fixed config defaults**, not as a full replacement for `config.yaml`.

High-level layering:
- `Config` (`domain/config/models.py`) provides workspace defaults.
- Live runtime state lives on the host agent / LLM / context manager.
- `SessionRuntimeState` (`domain/session/models.py`) captures the session-scoped overrides that should persist across save/resume.
- `session_store.py` persists conversations plus runtime overlay metadata and a `fingerprint`.
- `app/runtime/session_state.py` is the bridge that:
  - snapshots live runtime into `SessionRuntimeState`
  - restores config defaults for a fresh session
  - reapplies persisted session-scoped overrides when resuming

### Agent (`domain/agent/agent.py`)
The main orchestrator that coordinates LLM and tools.
- Manages conversation state (`messages`, `tokens`, `rounds`)
- Owns live runtime fields such as `active_mode`
- Carries session-scoped overlays like active model profile selections and approval overrides
- Registers and executes hooks

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
- Runtime `reconfigure()` is used when a resumed or switched model changes context budget

### Session Model and Persistence

#### Session domain types (`domain/session/models.py`)
Saved sessions persist:
- conversation messages
- token counters
- saved timestamp / preview metadata
- `fingerprint`
- `SessionRuntimeState`

`SessionRuntimeState` currently captures:
- active mode
- main model runtime identity
- active main model profile
- active sub model profile
- LLM debug trace
- session-scoped approval rule overrides (baseline approval default mode still comes from config)
- execution-target / remote-binding placeholders for later phases

#### Session store (`infrastructure/persistence/session_store.py`)
`SessionStore` is the JSON persistence layer for sessions.
- `save()` writes messages + runtime overlay + fingerprint and refreshes `saved_at`
- `list()` and `get_latest()` are fingerprint-aware by default
- latest ordering is based on most recently updated save time (`saved_at`), with file mtime fallback
- `load()` backfills missing token metadata for older sessions
- `append_system_message()` keeps diagnostics attached to the same saved session

Fingerprint rules in the current implementation:
- default local fingerprint is `local`
- `/sessions` only shows current-fingerprint sessions by default
- `/sessions all` shows all fingerprints
- auto-resume latest only searches within current fingerprint
- manual `/session <id>` may cross fingerprints, but must warn

### Session runtime bridge (`app/runtime/session_state.py`)
This module centralizes Phase 1 runtime/session semantics.

Key responsibilities:
- `build_session_runtime_state(...)`
  - snapshot current live runtime into serializable session state
- `restore_config_runtime_defaults(...)`
  - reset a fresh/live agent back to config defaults
  - restore default mode, default approval config, default model routing
- `apply_session_runtime_state(...)`
  - restore saved messages and token counters
  - reapply saved mode / model / debug / approval overrides

This split is important: config remains the baseline, while session state is a layered override.

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

### Tool Execution Pipeline (`domain/agent/tool_execution.py`)
`ToolExecutor.execute(tool_call)` follows a 10-step pipeline:

1. **Resolve tool** from agent-local list, fallback to global registry
2. **Build context** `BeforeToolExecuteContext` with metadata (source, server, schema, round)
3. **Guard hooks** (`BEFORE_TOOL_EXECUTE`) — fail-closed, first explicit deny stops execution
4. **Preflight validation** (`tool.preflight_validate`) — early argument checks
5. **Mode restrictions** — `is_tool_allowed_in_mode`, with mode-switch suggestions
6. **Approval check** — if guard marked `requires_approval`, call `approval_provider.request_approval()`
7. **Transform hooks** — modify args before execution
8. **Observer hooks** — pre-execution observation
9. **Execute tool** — run with transformed args
10. **Post-processing** — wrap in `AfterToolExecuteContext`, run transforms/observers, return result

Parallel execution: `execute_parallel()` uses `ThreadPoolExecutor(max_workers=8)` for multiple tool calls without approval provider.

### Backend Dispatch (`extensions/tools/backend.py`)
Tools support multiple execution backends:

- `@backend_handler("backend_id")` decorator registers handlers per backend
- `Tool.run_backend(...)` picks handler for current `backend_id`, falls back to `"local"`
- `LocalToolBackend` (`backend_id="local"`): direct local execution
- `RemoteRelayToolBackend` (`backend_id="remote"`): forwards to relay server

`ExecutionContext` carries runtime hints:
- `peer_id`: remote peer identifier
- `cwd`: current working directory
- `workspace_root`: workspace root path
- `execution_target`: "local" or "remote"
- `remote_stream_handler`: callback for streaming output chunks

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
- Manages MCP servers, skills service, and session restore/save lifecycle
- Computes the current session fingerprint from runtime/config
- Auto-resume latest is fingerprint-scoped and resolves "latest" by most recently saved/updated session
- Manual resume by explicit session id is allowed to cross fingerprints with warning

### Remote Exec Relay (host/peer)
Current remote execution flow is event-stream based and keeps rendering ownership on the host runtime.

- Transport endpoints (host HTTP service):
  - `POST /remote/chat/start`: create chat session and enqueue `chat_start`
  - `POST /remote/chat/stream`: long-poll event stream by cursor
  - `POST /remote/approval/reply`: resolve pending approval decisions
- Stream events include:
  - `chat_start`, `output`, `approval_request`, `approval_resolved`, `chat_end`, `error`
- Rendering model:
  - Host-side CLI renderer (`CLIRenderer`) is reused to produce terminal-formatted output chunks
  - Peer side prints host-provided terminal/plain chunks directly, with markdown fallback only when needed
- Slash commands in remote streaming mode are rendered through host `CLIRenderer` before being emitted as `output`, ensuring view panels (e.g. `/help`, `/model`, `/tokens`) are preserved remotely.

### Go Agent (`reuleauxcoder-agent/`)
Standalone Go binary for remote peer execution:

**Entrypoint** (`cmd/reuleauxcoder-agent/main.go`):
- CLI flags: `--host`, `--bootstrap-token`, `--cwd`, `--workspace-root`, `--poll-interval`, `--interactive`
- Registers with relay server using bootstrap token
- Runs heartbeat loop + poll loop for task dispatch

**Runner** (`internal/runner/runner.go`):
- `Register()` → obtain `peer_id` + `peer_token`
- Background heartbeat loop at configured interval
- Poll loop: long-poll for `RelayEnvelope` tasks
- Interactive mode: stdin chat proxied through host

**Protocol** (`internal/protocol/types.go`):
- `RegisterRequest/Response`, `Heartbeat`, `PollRequest`, `ResultRequest`
- `RelayEnvelope` wraps tool execution requests
- `ExecToolRequest/Result` for tool calls
- `ChatStartRequest`, `ChatStreamRequest/Response` for interactive chat
- `ApprovalReplyRequest` for remote approval decisions

**Tools** (`internal/tools/execute.go`):
- Supports: `shell`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`
- Shell: subprocess with timeout, stdout/stderr streaming
- File ops: path validation, content limits
- Glob/grep: skip `.git`, `node_modules`, `__pycache__`, venv, etc.

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

### Slash command scope conventions (Phase 1)
Slash command metadata should explicitly communicate scope in `ActionSpec.description` when the command mutates runtime or config.

Scope labels used in help text:
- `[session]`: affects only the current live session/runtime overlay; should not mutate workspace config
- `[global]`: persists workspace-level defaults/config and may also refresh current runtime
- `[local-only]`: only valid when running against local runtime/host capabilities
- `[session-index]`: operates on saved session inventory and fingerprint-visible session lookup

Canonical Phase 1 expectations:
- `/model use-main`, `/model use-sub`, `/model <profile>`: `[session]`
- `/model set-main`, `/model set-sub`: `[global]`
- `/approval set`: `[session]`
- `/approval set-global`: `[global]`
- `/mode ...`: `[session]`
- `/debug`, `/reset`, `/compact`, `/tokens`: `[session]`
- `/skills ...`: `[global]`
- `/mcp ...`: `[global][local-only]`
- `/sessions`, `/session latest|<id>`: `[session-index]` and fingerprint-aware
- `/save`, `/new`: `[session]`
- `/jobs ...`: `[session]`

Session runtime state currently restored by saved sessions includes:
- message history and token counters
- active mode
- active main/sub model profiles
- LLM debug trace
- approval runtime overrides
- session fingerprint

### Adding a new slash command
1. Create handler in `extensions/command/builtin/`
2. Register in command dispatcher
3. If the command changes state, annotate scope in `ActionSpec.description`
4. Keep session-vs-global semantics aligned with the Phase 1 conventions above

### Adding a new hook
1. Create class inheriting from `GuardHook`, `TransformHook`, or `ObserverHook`
2. Add `@register_hook(HookPoint, priority)` decorator
3. Implement `create_from_config(cls, config)` classmethod
4. Implement `run(self, context)` method
5. Import in `domain/hooks/builtin/__init__.py` and `discovery.py`

## Testing

Tests are in `tests/` mirroring the source structure.
Run with: `uv run python -m pytest tests/ -v`