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

- **agent/**: Agent core
  - `agent.py` — `Agent`: main orchestrator. Owns `AgentState` (messages, tokens, rounds), `active_mode`, thread-safe via `_state_lock` and `_stop_event` for cooperative cancellation. Key methods: `chat(user_input)` processes one turn (recovers dangling tool calls + injects sub-agent results first), `reset()` clears history, `set_mode()` switches mode. Agent-scoped hook registration via `register_hook()`/`list_hooks()` separate from global registry. `add_event_handler()` / `_emit_event()` powers UI reactivity.
  - `loop.py` — `AgentLoop`: manages the conversation loop. `_runtime_tail_message()` builds the `<system_context>` block each turn (UTC/local time, cwd, OS, Python version, shell). `_full_messages()` assembles system prompt per-mode with blocked tools, mode-switch hints, skills catalog. `run()` iterates up to max rounds with stop-check, streaming, token accumulation. Branches into sequential (with approval provider) or parallel (`execute_parallel()`) tool execution. Max-rounds triggers automatic summary prompt. Passes `round_index`, `active_mode`, `pending_tool_calls`, `summary_phase` as metadata to LLM.
  - `tool_execution.py` — `ToolExecutor`: 10-step execution pipeline (see Tool Execution Pipeline section below). `BeforeToolExecuteContext` carries `tool_source`, `mcp_server`, `tool_description`, `tool_schema`. Tool is re-resolved after transform hooks in case transforms mutate the tool call. `shell_cwd` propagation tracks working directory. Distinguishes `TypeError` (bad args) vs general `Exception` (exec failure).
  - `events.py` — `AgentEvent` dataclasses (`ContentDelta`, `ToolCallStart`, `ToolCallEnd`, `SubagentJobCompleted`, etc.) for event-driven UI updates.
- **config/**: Configuration domain models
  - `models.py` — `Config`, `ModeConfig` (tools allow/block lists, `allowed_subagent_modes`, `prompt_append`), `ApprovalConfig` (default mode + rule list), `ApprovalRule` (by `tool_name` or `tool_source`, actions: `allow`/`warn`/`require_approval`/`deny`).
  - `schema.py` — Pydantic-style validation schema with defaults, model profile validation, mode validation.
- **context/**: Context management
  - `manager.py` — `ContextManager`: token counting (tiktoken o200k_base), compression triggering, wall-hit detection, `reconfigure()` for model switches.
  - `compression.py` — compression strategies: `snip` (truncates old tool outputs) and `llm_summarize` (asks LLM to summarize), with `snip_keep_recent_tools` and `summarize_keep_recent_turns` config.
  - `summary.py` — `LLMSummarizer`: uses LLM to compress conversation history, preserving recent turns.
- **hooks/**: Hook system (see Hook System section below)
  - `types.py` — `HookPoint` enum, `GuardDecision` (allow/deny/abstain), `GuardHook`/`TransformHook`/`ObserverHook` base classes.
  - `registry.py` — `HookRegistry`: global hook store keyed by `HookPoint`, sorted by priority.
  - `discovery.py` — `discover_hook_specs()` scans `builtin/` for `@register_hook`-decorated classes.
  - `base.py` — `Hook` abstract base, `HookSpec` frozen dataclass for lazy instantiation.
  - `builtin/` — `ToolPolicyGuardHook`, `ToolOutputTruncationHook`, `ProjectContextHook`, `ProjectContextStartupNotifier`.
- **llm/**: LLM domain models
  - `models.py` — `LLMResponse` (content, tool_calls, usage, finish_reason), `ToolCall` (id, name, arguments), `TokenUsage`.
  - `messages.py` — `Message` types (system, user, assistant, tool), `ToolResult`.
  - `protocols.py` — `LLMClient` and `ApprovalProvider` protocols.
- **session/**: `SessionRuntimeState` captures mode, model profiles, debug trace, approval overrides, fingerprint. `SavedSession` wraps messages + state + metadata.
- **approval/**: `approval.py` — `ApprovalProvider` protocol (`request_approval()` → `ApprovalDecision`), `ApprovalRequest`/`ApprovalDecision` dataclasses. `approval_engine.py` — `ApprovalEngine`: evaluates rules (`match_tool_name`/`match_tool_source`), computes effective action with `default_mode` fallback, generates approval requests with validation.

### Services Layer (`services/`)
Coordinates domain objects and handles external interactions.

- **llm/**: LLM client and related services
  - `client.py` — `LLM`: OpenAI-compatible API client. Constructor params: `model`, `api_key`, `base_url`, `temperature`, `max_tokens`, `preserve_reasoning_content`, `backfill_reasoning_content_for_tool_calls`, `reasoning_effort`, `thinking_enabled`, `reasoning_replay_mode`, `reasoning_replay_placeholder`, `debug_trace`, `ui_bus`. Key methods: `chat(messages, tools, on_token, hook_registry, session_id, trace_id, metadata) → LLMResponse` — the main inference method that runs `BEFORE_LLM_REQUEST` guards/transforms/observers, streams via `on_token`, accumulates `content_parts`/`reasoning_parts`/tool call deltas via `tc_map`, then runs `AFTER_LLM_RESPONSE` transforms/observers. `reconfigure(**kwargs)` hot-swaps model/client settings at runtime (for model profile switches). `_call_with_retry()` performs inline retry with exponential backoff (max 3 retries). Debug trace writes `llm_trace_{ts}_{session}_{trace}.json` to diagnostics dir. On exception calls `persist_llm_error_diagnostic()` and attaches `llm_diagnostic_path` to the exception.
  - `sanitizer.py` — `sanitize_messages_for_llm()`: message repair layer. Handles missing `tool_call_id` backfill, missing tool output injection, `reasoning_content` preservation/removal/backfill. Dispatches by `reasoning_replay_mode`: `"none"` (strip all reasoning) vs `"tool_calls"` (inject placeholders for tool-call assistant messages). Controlled by profile flags: `preserve_reasoning_content`, `backfill_reasoning_content_for_tool_calls`, `thinking_enabled`. Uses `DEFAULT_REASONING_REPLAY_PLACEHOLDER = "[PLACE_HOLDER]"`.
  - `factory.py` — `build_llm_from_settings(settings, *, debug_trace) → LLM`: creates LLM from config/profile objects. `reconfigure_llm_from_settings(llm, settings, *, debug_trace)`: reconfigures existing LLM. `llm_runtime_kwargs()` extracts 12 canonical `_LLM_RUNTIME_FIELDS` from settings.
  - `diagnostics.py` — `snapshot_messages(messages, limit=10)`: builds compact tail snapshot. `persist_llm_error_diagnostic()`: writes `llm_error_{ts}_{session}.json` with error details, message tails, tool schemas, model info.
  - `retry.py` — standalone retry decorator (separate from the inline retry in `LLM.chat()`).
- **prompt/**: System prompt builder
  - `builder.py` — `PromptAssembler`: constructs system prompt from `PromptBlock` objects. `PromptBlock` has `zone` (`STATIC` or `SEMI_STATIC` for cache stability), `order`, `key`, `content` callable. Sorted by `(zone, order, key)` for deterministic output. Blocks include identity/rules, mode context, tool schemas, skills catalog, user injection.
- **config/**: Configuration loading and validation
  - `loader.py` — `ConfigLoader`: loads `config.yaml`, merges with defaults (`DEFAULT_CONFIG`), validates via `ConfigSchema`, resolves model profile references. Provides `load_workspace_config()` and `load_user_config()`. Supports `WORKSPACE_CONFIG_PATH = ".rcoder/config.yaml"` and `USER_CONFIG_PATH = "~/.rcoder/config.yaml"`.

### Infrastructure Layer (`infrastructure/`)
External resource access and low-level utilities.

- **platform.py**: `PlatformInfo` singleton — system detection (`is_windows`, `is_linux`, `is_darwin`, `system`), preferred shell detection (`get_preferred_shell()` → `ShellType` enum: `BASH`/`POWERSHELL`/`POWERSHELL_CORE`/`CMD`/`UNKNOWN`), shell executable resolution (`get_shell_executable()`), path formatting. Module-level conveniences: `get_platform_info()`, `is_windows()`, `get_shell_type()`, `get_shell_command()`.
- **fs/paths.py**: Path utility functions — `get_sessions_dir()`, `get_history_file()`, `get_user_config_dir()` → `~/.rcoder`, `get_tool_outputs_dir(configured_dir=None)` → `.rcoder/tool-outputs`, `get_diagnostics_dir()` → `.rcoder/diagnostics`, `ensure_user_dirs()` creates all directories.
- **persistence/**: Storage layer
  - `session_store.py` — `SessionStore`: JSON persistence for sessions. `save()` writes messages + runtime overlay + fingerprint, refreshes `saved_at`. `list()` and `get_latest()` are fingerprint-aware. `load()` backfills missing token metadata for older sessions. `append_system_message()` attaches diagnostics. `generate_session_id()` creates `session_<timestamp>_<hex>` IDs. Thread-safe via `threading.RLock()`. `DEFAULT_SESSION_FINGERPRINT = "local"`.
  - `workspace_config_store.py` — `WorkspaceConfigStore`: persists runtime config changes to workspace `config.yaml`. Methods: `save_approval_config()`, `save_active_model_profile()`, `save_active_sub_model_profile()`, `save_active_mode()`, `save_mcp_server_config()`. Used by `[global]` slash commands.
  - `skills_config_store.py` — `SkillsConfigStore`: persists disabled skills list to workspace config.
- **yaml/**: YAML loading with error context (file path, line numbers).
- **openai/**: OpenAI SDK wrapper for typed client instantiation.

### Interfaces Layer (`interfaces/`)
User-facing interfaces.

- **Root abstractions** (`interfaces/`):
  - `events.py` — `UIEventBus`: dual-mode pub/sub bus. **Synchronous mode** (default): `emit()` calls handlers directly on the caller's thread. **Queued mode**: pass a `queue.Queue` at construction; `emit()` pushes events onto the queue, and a separate `drain()` call dequeues and dispatches on the UI thread (used by sub-agents to batch-deliver events to the parent renderer). `UIEvent` dataclass: `message`, `level` (`UIEventLevel`: INFO/SUCCESS/WARNING/ERROR/DEBUG), `kind` (`UIEventKind`: SYSTEM/COMMAND/SESSION/MODEL/MCP/APPROVAL/VIEW/AGENT/CONTEXT/REMOTE), `timestamp`, `data` dict. `AgentEventBridge` translates domain `AgentEvent` → `UIEvent`. Bus has `info()`/`success()`/`warning()`/`error()`/`debug()`/`open_view()`/`refresh_view()` convenience methods.
  - `interactions.py` — `UIInteractor` Protocol: `notify()`, `confirm()`, `choose_one()`, `input_text()`, `review()`. Request/response dataclasses: `ConfirmRequest/Response`, `ChoiceItem`/`ChooseOneRequest/Response`, `InputTextRequest/Response`, `ReviewRequest/Response`.
  - `ui_registry.py` — `UICapability` enum (TEXT_INPUT, STREAM_OUTPUT, PALETTE, BUTTONS, MENUS, TABS, MODAL, DIFF_REVIEW, TEXT_SELECT, TEXT_EDIT), `UIProfile` (frozen dataclass: `ui_id`, `display_name`, `capabilities`), `UIRegistration` bundles `UIProfile` + `ViewRendererRegistry` + `UIInteractor`, `UIRegistry` maps `ui_id → UIRegistration`.
  - `view_registry.py` / `view_registration.py` — `ViewRenderer` protocol, `ViewRendererSpec` (`view_type` + `render` callable), `ViewRendererRegistry` maps `view_type → ViewRendererSpec`, `@register_view(view_type=..., ui_targets=...)` decorator for declarative registration.
- **cli/**: CLI implementation (Rich-based)
  - `main.py` — CLI entrypoint (`main()`), parses args, starts REPL.
  - `args.py` — `CLIArgs` dataclass: `--config`, `--model`, `--prompt`, `--resume`/`-r`, `--server`, `--version`.
  - `repl.py` — `CLIREPL`: main REPL loop using `prompt_toolkit`. Handles input, history, slash command dispatch, session save/restore. `_dispatch_input()` routes user text to agent `chat()` or command handlers.
  - `commands.py` — `CommandDispatcher`: parses leading `/` commands, routes to registered `ActionSpec` handlers via `ActionRegistry`.
  - `render.py` — `CLIRenderer`: event-driven agent/UI renderer. Stream rendering accumulates tokens into `_ContentBlock`, flushes completed markdown blocks using `_find_committed_boundary()` — a markdown-it block-token parser that confirms every block except the last (potentially incomplete) one, preventing orphaned code fences when a `\n\n` appears inside a fenced code block. Tool call rendering shows `name(args)` in cyan panel; compact output has two branches: **pre-truncated** (`[truncated]` prefix) preserves hook stats + archive path and shows first 3 + last 3 lines; **non-truncated** caps at 1200 chars/5-20 lines and appends total line count. `display` text is `_escape_markup`-wrapped to prevent Rich from interpreting bracket notation as markup tags. Diff rendering uses `Syntax("diff", theme="monokai")`. Sub-agent rendering shows status panels. Slash command results rendered as Rich panels (`/help`, `/model`, `/tokens`, etc.).
  - `interactor.py` — `CLIInteractor`: implements `UIInteractor` with `prompt_toolkit` dialogs (`confirm()`, `choose_one()`, etc.).
  - `approval.py` — `CLIApprovalProvider`: interactive approval prompts for tool execution.
  - `registration.py` — `CLIRegistration`: wires up CLI-specific `UIRegistration` with `CLIRenderer` + `CLIInteractor`.
  - `views/` — View renderers registered via `@register_view`. `registry.py` finds and instantiates view specs.
- **shared/** (`interfaces/shared/`): Cross-interface utilities.
  - `approval_preview.py` — `build_preview_diff()`: performs filesystem I/O (read file, compute diff) to build approval preview text. Lives in interface layer rather than domain because it depends on filesystem access. Used by both CLI and TUI approval handlers.
- **tui/**: TUI interface.
  - `approval_handler.py` — `TUIApprovalHandler`: interactive approval prompts for the Textual-based TUI. Shares `build_preview_diff` with CLI via `interfaces/shared/`.
- **entrypoint/**: Application bootstrap
  - `runner.py` — `AppRunner`: application initialization and DI (see AppRunner detail below).
  - `dependencies.py` — `AppDependencies`: customizable component factory for testability.
  - `session_lifecycle.py` — session save/restore orchestration during startup/shutdown.
  - `remote_relay.py` — bootstraps the HTTP relay server when `--server` mode is active.

### Extensions Layer (`extensions/`)
Pluggable extension system.

- **tools/**: Built-in tools and tool infrastructure
  - `base.py` — `Tool` base class: `name`, `description`, `parameters` (OpenAI function schema dict), `execute(**kwargs) → str`, `preflight_validate(args)`. `__init_subclass__` auto-discovers `@backend_handler` methods via MRO walking. `schema()` returns `{"type": "function", "function": {...}}`. `run_backend(...)` picks handler for current `backend_id`, falls back to `"local"`.
  - `registry.py` — `@register_tool` decorator populates `_TOOL_CLASSES`. `build_tools(backend)` instantiates all registered tools. `get_tool(name, backend)` creates single tool by name. `iter_tool_classes()` returns classes after lazy-importing `reuleauxcoder.extensions.tools.builtin`.
  - `backend.py` — `ToolBackend` base (marker with `backend_id = "base"`). `LocalToolBackend` (`"local"`): direct execution. `RemoteRelayToolBackend` (`"remote"`): forwards to relay server. `ExecutionContext` carries `peer_id`, `cwd`, `workspace_root`, `execution_target` ("local"/"remote"), `remote_stream_handler`.
  - `policies/` — `ToolPolicy` Protocol: `evaluate(tool_call) → GuardDecision | None`. `DEFAULT_TOOL_POLICIES` tuple starts with `ShellDangerousCommandPolicy` (blocks rm -rf, fork bombs, curl|bash, etc.).
  - `builtin/shell.py` — `ShellTool`: platform-aware shell detection via `infrastructure.platform`, stale CWD detection (resets if dir gone), PowerShell fallback (`_run_powershell()`), output truncated at ~9k chars (keeps head+tail).
  - `builtin/read.py` — `ReadFileTool`: supports `offset`/`limit` paging; `override=true` reads full file.
  - `builtin/write.py` — `WriteFileTool`: `_unified_diff()` produces diff output truncated at 3k chars.
  - `builtin/edit.py` — `EditFileTool`: `_validate_edit_request()` checks file existence and old_string uniqueness before applying.
  - `builtin/grep.py` — `GrepTool`: skips `.git`, `node_modules`, `__pycache__`, `.venv`, etc. via `_SKIP_DIRS`.
  - `builtin/glob.py` — `GlobTool`: recursive `**` support, skip-dirs filter.
  - `builtin/agent.py` — `AgentTool`: spawns sub-agents. `_parent_agent` ref for mode validation. Pre-flight: mutual exclusion of `task`/`tasks`, batch requires `mode='explore'` + `run_in_background=true`, `parallel_explore` caps (1-4).
- **command/**: Slash commands with `@register_command_module` + `ActionRegistry`
  - Complete command list: `/help`, `/quit`/`/exit`, `/reset`, `/new`, `/compact [force <strategy>]`, `/tokens`, `/debug on/off`, `/save`, `/model <profile|use-main|use-sub|set-main|set-sub>`, `/mode <mode>`, `/skills [reload|enable|disable <n>]`, `/mcp [show|enable|disable <s>]`, `/approval [show|set ...|set-global ...]`, `/sessions [all]`, `/session <id|latest>`, `/jobs [get|wait <job_id>]`.
  - Commands grouped by module: `system.py` (help/quit/reset/compact/tokens/debug), `sessions.py` (save/new/sessions/session), `model.py` (model switch), `mode.py` (mode switch), `skills.py`, `mcp.py`, `approval.py`, `subagent_jobs.py`.
- **mcp/**: MCP server integration — `MCPManager` (independent asyncio loop in background thread), `MCPClient` (stdio JSON-RPC with reconnect-once), `MCPTool` (schema translation wrapping remote tools). Config per-server: `command`, `args`, `env`.
- **skills/**: Skills system — `SkillsService` discovers skills from `SKILL.md` files, builds catalog with `Skill` objects (`name`, `description`, `location`). Supports enable/disable with persistence via `SkillsConfigStore`.
- **subagent/**: `SubagentManager` — `_VALID_SUBAGENT_MODES = frozenset({"explore", "execute", "verify"})`, `_DEFAULT_MAX_ROUNDS = 50`, `_DEFAULT_TIMEOUT_SECONDS = 300`, `_MAX_TIMEOUT_SECONDS = 3600`. `SubagentJob` dataclass tracks `job_id`, `status` (PENDING/RUNNING/COMPLETED/FAILED), `start_time`/`end_time`, `result`/`error`. Approval for sub-agents uses a judge-middleware pattern: `build_subagent_approval_provider()` returns `SharedApprovalProvider(handler=..., judges=[ParentLLMJudge(...)])` where `ParentLLMJudge` (a callable `ApprovalJudge`) delegates to the parent LLM first, and only escalates to the human handler if the judge returns `None`. Approval lock (RLock) wraps the handler to serialise terminal access across sub-agents. Manager takes `default_timeout_seconds` and `max_timeout_seconds` as injectable params.
- **remote_exec/**: `RemoteExecService` — HTTP relay server with `POST /remote/chat/start`, `POST /remote/chat/stream` (long-poll event stream), `POST /remote/approval/reply`. Stream events: `chat_start`, `output`, `approval_request`, `approval_resolved`, `chat_end`, `error`. Bootstrap via `sh -c 'curl -fsSL ... | sh'` with one-time token.

### Application Layer (`app/`)
Use-case orchestration and shared runtime utilities.

#### `app/commands/` — Cross-Interface Command System
UI-agnostic command infrastructure shared by CLI, TUI, and other interfaces.

- `specs.py` — `ActionSpec` (frozen): central action definition with `action_id`, `feature_id`, `description`, `triggers` (tuple of `TriggerSpec`), `parser` callable, `handler` callable. `TriggerSpec` has `kind` (`SLASH`/`PALETTE`/`BUTTON`/`MENU`/`SHORTCUT`), `value`, `ui_targets`, `required_capabilities`. An action can have multiple triggers gated by UI capabilities.
- `registry.py` — `ActionRegistry`: static, explicit registry (not singleton). `register(action)` / `register_many(actions)`. `parse(user_input, ui_profile)` filters actions by UI availability, checks SLASH triggers, calls parsers, returns `ParsedAction` on first match. `dispatch(parsed, ctx)` calls the handler.
- `models.py` — `CommandContext` (runtime context for handlers: `agent`, `config`, `ui_bus`, `ui_interactor`, `skills_service`, etc.). `CommandResult`: return type with `action` (`"continue"`/`"chat"`/`"exit"`), `notifications`, `view_requests`. `"chat"` action signals the caller to inject payload into the LLM conversation.
- `parser.py` / `dispatcher.py` — thin entry points: `parse_command()` / `dispatch_command()`.
- `module_registry.py` — `@register_command_module` decorator collects `register_actions(registry)` functions for lazy discovery.
- `loader.py` — `load_command_modules()` imports all `builtin/` modules and calls their `register_actions`.
- `matchers.py` — `match_commands()` returns candidate `ActionSpec` matches for a given input prefix (used by auto-complete).
- `params.py` — parameter extraction utilities for command arguments.
- `help.py` — builds help text markdown from all registered actions, grouped by `feature_id`, annotated with scope labels.

#### `app/runtime/` — Runtime State & Approval Views

- `session_state.py` — bridge between live agent runtime and persisted `SessionRuntimeState`:
  - `build_session_runtime_state()` snapshots current runtime into serializable state.
  - `restore_config_runtime_defaults()` resets agent back to config defaults.
  - `apply_session_runtime_state()` reapplies saved mode/model/debug/approval overrides.
  - `merge_approval_config()` clones baseline rules, replaces with session-scoped overrides matching the same target.
- `approval.py` — approval view models:
  - `ApprovalView` aggregates `default_mode`, per-rule `ApprovalRuleView` list, per-tool `ApprovalToolPolicyView` list, effective MCP policies, and editor hint. `to_payload()` produces the full view with computed markdown.
  - `build_approval_view()` / `build_approval_markdown()` construct views from config + agent state.
  - `refresh_approval_runtime()` syncs approval config to the live `ToolPolicyGuardHook` runtime hook.
  - `parse_approval_target()` / `find_matching_rule()` support the `/approval set` command's target resolution.

#### `app/usecases/` — (placeholder for future use-case orchestration)

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

- **State**: `AgentState` dataclass holds `messages`, `total_prompt_tokens`, `total_completion_tokens`, `current_round`.
- **Thread safety**: `_state_lock: threading.Lock()` for state mutations, `_stop_event: threading.Event()` for cooperative cancellation mid-turn (used by stop/interrupt).
- **Event pub-sub**: `_event_handlers` list, `add_event_handler()`, `_emit_event()` powers UI reactivity — content deltas, tool call start/end, sub-agent completions, errors.
- **Core flow**: `chat(user_input)` is the single-turn entry point:
  1. Appends user message to history
  2. Recovers dangling tool calls via `_collect_pending_tool_calls()` + `reconcile_pending_tool_calls(reason)` (fallback after crash/interruption, preserving message parity)
  3. Injects completed sub-agent results via `_inject_completed_subagent_jobs()`
  4. Delegates to `AgentLoop.run()`
- **Sub-agent integration**: `inject_subagent_job_result(job)` feeds background sub-agent results back into parent conversation.
- **Mode/tool queries**: `get_active_tools()`, `get_blocked_tools()`, `is_tool_allowed_in_mode(tool_name)`, `suggest_modes_for_tool(tool_name)`, `set_mode(mode_name)`.
- **Agent-scoped hooks**: `register_hook(hook_point, hook)`, `list_hooks(hook_point)` — separate from the global `HookRegistry`.
- **Lifecycle**: `reset()` clears conversation history (used by `/reset`). Context manager initialized from `config.context` with snip/summarize parameters.

### AgentLoop (`domain/agent/loop.py`)
Manages the conversation loop.

- `_runtime_tail_message()` builds the `<system_context>` block injected each turn: UTC time, local time, working directory, OS/Kernel (`uname -srm`), Python version, shell.
- `_full_messages()` assembles the full message list: system prompt (per-mode, blocked tools, mode-switch hints, skills catalog) + conversation history + ephemeral tail.
- `run()` iteration:
  - Compression before loop start (`maybe_compress`)
  - Max-round iteration with `_stop_event` checks
  - Streaming via `on_token` callback to UI
  - Token count accumulation from LLM response
  - Branches: sequential tool execution (when approval provider present) or parallel `execute_parallel()` (no approval). Max-rounds triggers automatic summary prompt via `_build_summary_prompt()`.
- **Metadata passed to LLM**: `round_index`, `active_mode`, `pending_tool_calls`, `summary_phase` — used for diagnostic tracing.
- `last_response_streamed` flag controls whether final response is rendered again by UI.

### ContextManager (`domain/context/manager.py`)
Manages conversation context and token limits.

- Token counting with tiktoken (o200k_base encoding).
- Compression strategies when approaching limits:
  - `snip`: truncates old tool outputs, keeping `snip_keep_recent_tools` recent calls (configurable, default 5). Min thresholds: `snip_threshold_chars` (default 1500), `snip_min_lines` (default 6).
  - `llm_summarize`: asks LLM to compress history, preserving `summarize_keep_recent_turns` recent turns (default 5). Uses `LLMSummarizer` (`domain/context/summary.py`).
- Wall hit detection for context boundaries (when even compressed context exceeds limit).
- `reconfigure(max_context_tokens)` adjusts budget at runtime for model switches or session resume.
- Compression is triggered before each loop iteration by `AgentLoop.run()`.

### Approval Engine (`domain/approval.py` + `domain/approval_engine.py`)
Manages tool approval decisions.

- **Protocols**: `ApprovalProvider` — `request_approval(request: ApprovalRequest) → ApprovalDecision`. `ApprovalRequest` carries `tool_name`, `arguments`, `reason`, `source` (local/mcp). `ApprovalDecision` is `ALLOW`/`DENY`/`ABSTAIN` with optional `reason`. `ApprovalJudge` — `Callable[[ApprovalRequest], ApprovalDecision | None]`: optional pre-handler judges that short-circuit before the human handler; returning `None` escalates to the next judge or handler.
- **SharedApprovalProvider**: single implementation used by both CLI (via handler injection) and sub-agents (via handler + judges). Constructor takes `handler: ApprovalHandler` + optional `judges: list[ApprovalJudge]`. `request_approval()` iterates judges first; a judge returning a non-`None` decision short-circuits. If all judges return `None`, falls through to the handler (typically human interaction). Exposes `handler` property for sub-agent delegation.
- **Engine** (`ApprovalEngine`): evaluates ordered approval rules. Each `ApprovalRule` matches by `tool_name` or `tool_source` (e.g. `"mcp"`, `"mcp:<server>"`, `"mcp:<server>:<tool>"`). Actions: `allow`/`warn`/`require_approval`/`deny`. Falls back to `default_mode` when no rule matches.
- **Integration**: `ToolPolicyGuardHook` evaluates tool policies during `BEFORE_TOOL_EXECUTE`. If result is `requires_approval`, the ToolExecutor calls the configured `approval_provider` (CLI interactive, sub-agent delegated with judge middlewares, or remote forward).

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

Workplace config at `.rcoder/config.yaml`, user defaults at `~/.rcoder/config.yaml`.

Key sections:

- **`models`**: `active`, `active_main`, `active_sub` select profiles. Each `profiles.<name>` has:
  - Core: `model`, `api_key`, `base_url`, `max_tokens`, `temperature`, `max_context_tokens`.
  - Reasoning: `thinking_enabled`, `reasoning_effort` (`"high"`/`"medium"`/`"low"`), `reasoning_replay_mode` (`"tool_calls"`/`"none"`), `preserve_reasoning_content`, `backfill_reasoning_content_for_tool_calls`.
- **`modes`**: `active` selects current mode. Each `profiles.<mode>` has `description`, `prompt_append`, `tools` (allow list; `"*"` for all), `allowed_subagent_modes`.
- **`approval`**: `default_mode` (`"allow"`/`"warn"`/`"require_approval"`/`"deny"`). `rules`: list of `{tool_name, tool_source, action}`. Tool source matches support `"mcp"`, `"mcp:<server>"`, `"mcp:<server>:<tool>"`.
- **`prompt`**: `system_append` for custom system prompt injection.
- **`skills`**: `enabled` (bool), `disabled` (list of skill names to exclude), `dirs` (additional skill directories).
- **`mcp.servers`**: `<name>` → `{command, args, env}`. MCP servers are stdio-based JSON-RPC subprocesses.
- **`context`**: optional compression tuning — `snip_keep_recent_tools` (default 5), `snip_threshold_chars` (1500), `snip_min_lines` (6), `summarize_keep_recent_turns` (5).
- **`session`**: `auto_save` (default true), `dir` (default `.rcoder/sessions`).
- **`tool_output`**: `max_chars` (default 12000), `max_lines` (120), `store_full_output` (true), `store_dir` (`.rcoder/tool-outputs`).
- **`remote_exec`**: `enabled`, `host_mode`, `relay_bind`, `bootstrap_access_secret`, `bootstrap_token_ttl_sec`, `peer_token_ttl_sec`, `heartbeat_interval_sec`, `heartbeat_timeout_sec`, `default_tool_timeout_sec`, `shell_timeout_sec`.
- **`cli`**: `history_file` (default `~/.rcoder/history`).

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
2. Set `name`, `description`, `parameters` as class attributes and implement `execute(**kwargs) -> str`
3. Use `@register_tool` decorator on the class for auto-discovery via `build_tools(backend)`
4. `schema()` has a default implementation — override only if needed
5. For multi-backend tools, decorate handler methods with `@backend_handler("backend_id")`

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
1. Create or update a module in `extensions/command/builtin/`
2. Define a `register_actions(registry: ActionRegistry)` function decorated with `@register_command_module`
3. Inside, call `registry.register_many([ActionSpec(action_id=..., feature_id=..., description=..., triggers=[...], handler=..., parser=...)])`
4. If the command changes state, annotate scope in `ActionSpec.description`
5. Keep session-vs-global semantics aligned with the Phase 1 conventions above

### Adding a new hook
1. Create class inheriting from `GuardHook`, `TransformHook`, or `ObserverHook`
2. Add `@register_hook(HookPoint, priority)` decorator
3. Implement `create_from_config(cls, config)` classmethod
4. Implement `run(self, context)` method
5. Import in `domain/hooks/builtin/__init__.py` and `discovery.py`

## Testing

Tests are in `tests/` mirroring the source structure.
Run with: `uv run python -m pytest tests/ -v`

## Tooling

This project uses **uv** as its package manager and task runner.
- `uv run ...` — run any command in the project virtual environment
- `uv sync` — sync dependencies
- `uv add <pkg>` — add a dependency