# ReuleauxCoder Project Context

This file describes the current repository, not a future design. Detailed design history and acceptance evidence live under `references/`.

## Current snapshot

- Package version: `0.11.1`.
- Python interface: `rcoder-cli` is the linear CLI with native terminal scrollback, Rich output and prompt_toolkit line editing over JSON-RPC.
- Launcher: `rcoder` auto-selects the bundled TUI when Node >=22 and a terminal are available; `rcoder-tui` requires it explicitly. Batch/backend modes use CLI. Wheel and sdist releases contain the standalone JS bundle; runtime installation needs no npm.
- Independent TUI: `reuleauxcoder-tui/` is a React + Ink frontend over stdio JSON-RPC, with top-level slash menus, backend-owned command panels and a persistent composer. Launch it with `node reuleauxcoder-tui/dist/cli.js` after building.
- TUI navigation: wheel reports scroll independently of draft/history and menu selection; arrows edit visual input lines or select menu items. Reading stays paused through new output and resize until explicitly resumed. Key rules and reproducible responsiveness measurements: `docs/tui-navigation.md`.
- TUI rendering: a version/hash-checked Ink adapter reuses bounded parsing/width caches and composed rows, with equal-style ANSI fast paths. Animation timing is unchanged; `reuleauxcoder-tui/renderer/README.md` describes installation and upgrade checks, and `docs/tui-rendering-performance.md` records measurements.
- Remote peer: `reuleauxcoder-agent/`, a CLI-only Go peer.
- VS Code extension: `reuleauxcoder-vscode/` runs in the workspace extension host on local/Remote workspaces, owns a persistent stdio core and exposes a right-side conversation, native diff approvals, editor context and byte uploads. It ships Chinese/English UI resources and a compatible core wheel; its README documents build and host/browser tests.
- Runtime supports sessions, approvals, hooks/extensions, skills, MCP, subagents, LSP, local/remote tools, streaming output, and context compression.

## Repository map

```text
reuleauxcoder/
├── app/             # command use cases and runtime orchestration
├── domain/          # agent, events, ports, hooks, sessions, policies
├── services/        # LLM, prompt and config services
├── infrastructure/  # local workspace/process/storage/platform adapters
├── extensions/      # tools, commands, LSP, MCP, skills, subagent, remote exec
├── presentation/    # UI-neutral reducer, cells, policies and semantics
├── interfaces/      # CLI/TUI adapters, interface ports, bootstrap
└── compat/          # compatibility helpers

reuleauxcoder-agent/
├── cmd/reuleauxcoder-agent/
└── internal/{client,process,protocol,runner,terminal,tools,workspace}/

reuleauxcoder-tui/
├── src/{state,ui}/
├── src/{cli.tsx,profile.ts}
└── test/            # real Python runtime, Ink input and PTY integration

reuleauxcoder-client/
├── src/             # host-neutral TypeScript runtime client and wire contracts
└── src/node/        # stdio framing and frontend-local file reads
```

Layer rules:

- TypeScript frontends share the private, zero-runtime-dependency `@reuleauxcoder/client` package. Its root entry is browser-safe; `/node` is opt-in. Frontends supply their own UI profile. TUI install/build hooks compile the local dependency with the existing toolchain; see `reuleauxcoder-client/README.md`.
- Domain and presentation code must not import Rich, prompt_toolkit, Textual, or CLI view code.
- Domain runtime imports must not reach app, extensions, infrastructure or services. Tool contracts and prompt policy live in domain; integration hooks live in `extensions/hooks`. Host factories supply the native shell name and reconstruct subagent services after restoring core history.
- Commands return `CommandEffect`, typed view models, interaction requests, and state changes; they do not construct Rich objects.
- Tools use `WorkspacePort` and `ProcessPort` primitives. Platform-specific behavior belongs in local or remote adapters.
- CLI and TUI are adapters over the same runtime events, presentation semantics, command effects, and interaction ports.

## Agent and runtime

`domain/agent/agent.py` owns conversation state, session generation, active mode, stop state, lifecycle coordination, hook scope, and subagent result injection.

`domain/agent/loop.py` owns the LLM/tool round loop, context compression checks, token accounting, tool-call adjacency, and max-round handling. `request_projection.py` owns runtime-tail context, prompt/schema caches and provider replay projections through `RequestProjectionHost`; `RequestProjectionState` holds session-scoped replay state.

Canonical provider messages use exactly one leading `system` message. Project context, summaries, resume/runtime updates, subagent data, diagnostics and the volatile execution state are application-generated synthetic `user` messages with reserved provenance tags documented by that fixed system prompt. The provider boundary fail-closes legacy or extension-injected later system messages into `<legacy_runtime_context>`. Only nested/standalone runtime-instruction regions receive runtime-control authority; file, tool, note, Git, LSP and delegated payloads remain untrusted data.

## LLM providers and request modes

`domain/llm/models.py` and `protocols.py` define the provider-neutral response and client surface. `services/llm/client.py` owns normalized streaming, retry/cancellation, tool argument parsing, usage accounting and runtime reconfiguration. `services/llm/providers.py` owns wire adapters. Supported provider/request-mode pairs are:

- `openai-compatible` + `chat-completions` (the compatibility default);
- `openai-compatible` + `responses` (explicit opt-in);
- `anthropic` + `messages` (the Anthropic default and only valid mode).

Omitting `request_mode` preserves the provider default for old configuration. Conversation history remains locally authoritative and provider-neutral. Responses mode does not chain upstream response IDs: every round rebuilds the full request, sends `store=false`, and retains Responses-native reasoning/function-call output items under assistant `provider_data` for exact replay. Other wire adapters ignore or strip that private field, so a saved conversation can switch request modes or providers without leaking unsupported wire data.

The volatile execution overlay is rebuilt for each request and is never appended to canonical history. In Responses mode it is converted to a trailing `developer` input item after the stable conversation. A model/session-derived `prompt_cache_key` remains stable across rounds; explicit cache mode marks the last stable text or tool-output block as the cache breakpoint, while implicit mode delegates placement to the provider. Encrypted reasoning content is requested and replayed through `provider_data` rather than flattened into visible history.

`domain/agent/tool_execution.py` is the shared tool pipeline:

Result validation, bounded metadata projection and failure aggregation live in
`domain/agent/tool_outcome_validation.py`; the executor retains ordering,
approval and effect delivery. Session payload validation lives in
`infrastructure/persistence/session_validation.py`, separate from file I/O and
writer coordination.

1. resolve the scoped tool;
2. build a typed execution context;
3. run authorization guards;
4. preflight arguments and mode restrictions;
5. request approval when required;
6. run argument transforms/observers;
7. execute through the selected backend/ports;
8. run outcome transforms/observers;
9. emit a structured `ToolOutcome` and runtime events.

`domain/runtime/events.py` defines serializable, correlated runtime events. `presentation/reducer.py` reduces transcript cells; `presentation/execution.py` independently reduces Plan, progress, agent activity and Attention into `ExecutionViewState`. `presentation/policy.py` controls human-facing folding and verbosity independently of model-context truncation.

The agent receives complete tool output unless the model-context truncation hook applies. CLI live output and final history are separate bounded views of that output.

Tool start/finish runtime facts are ledgered independently of model/UI text, including status, exit code, timeout/error kind, truncation and archive checksum. Subagent verification derives objective evidence and failure state from these events rather than trusting the child's prose.

Live session persistence writes the first snapshot synchronously so a new session is discoverable before its first reply. `domain/output_journal.py` checkpoints response/reasoning/tool chunks every second or 64 Ki characters and immediately records final tool output. Message commits acknowledge matching stream IDs in the same durable event. Recovery projects unacknowledged output into the human transcript, without changing provider messages or automatically replaying work. Torn ledger tails are separated before new appends; snapshots fsync before replacement and sync directories on POSIX. Unsent frontend drafts remain separate from backend persistence.

Fresh session stores reuse request/checkpoint files only after checking their exact serialized bytes; changed artifacts retain atomic replacement and fsync. Shutdown waits for the backend's durable save without a fixed RPC deadline, with `runtime.shutdown_progress` notifications for TUI phases and a continuous elapsed timer. Explicit forced exit requires two interrupts during shutdown and warns that saving may be incomplete.

## Workspace and process primitives

- `domain/workspace.py`: `WorkspacePort` and filesystem result types.
- `domain/process.py`: `ProcessPort`, process request/result, timeout/cancel and stream callbacks.
- `infrastructure/workspace/local.py`: confined local filesystem adapter.
- `infrastructure/process/local.py`: local subprocess adapter with concurrent stdout/stderr draining, cancellation, timeout, and partial-output preservation.
- `extensions/remote_exec/backend.py`: remote tool backend that forwards the same workspace/process primitives.

`grep` performs bounded streaming search with Git-owned ignore selection when available. Local matching uses timed Python-compatible regex; peers advertising `workspace.fs.search_text.bounded` perform Go regex matching remotely in one request. Older peers require an upgrade for grep; there is no per-file download fallback. Limits, syntax differences and benchmarks are documented in `docs/search.md`.

Product tools in `extensions/tools/builtin/` compose those primitives. The Go peer does not own a second product-tool policy layer:

- protocol v2 exposes workspace and process primitives;
- legacy protocol v1 accepts shell only and adapts it to the same process manager;
- read/write/edit/list behavior is driven by host tools through remote workspace operations;
- approval, display, diff policy, retention and LSP decisions remain host-owned.

Output retention is tool-directed through `ToolRetentionHint`: read uses head/anchor semantics, shell uses tail semantics, and search/list tools may use head-tail. Timeout/cancel outcomes keep partial output; the CLI shows a rolling five-line live tail while the agent retains the full result subject to context policy.

Managed shell processes are published before the initial wait, so interrupting a turn or a poll leaves started processes available through `/ps` and `shell_session`. The initial yield defaults to five seconds; runtime timeout defaults to zero (no deadline), with positive values retaining process-tree termination. Legacy remote peers require an explicit positive timeout. Shutdown still cleans up session-owned processes. The TUI keeps bounded per-stream output tails, displays active processes and elapsed time in a height-budgeted sidebar, and folds consecutive polls into one waiting surface; expanded output preserves individual calls.

## CLI and presentation

`/shell` selects an in-memory preference on the local process backend. The command-owned panel shows native executable names and paths; Windows also lists WSL distributions and probes only the chosen distribution. New pipe/PTY processes use the selection, model descriptions/runtime context reflect it, and scoped backends inherit an independent snapshot. Existing processes are unchanged. Remote peers retain their own native shell. See `docs/shell-selection.md`.

The independent React TUI lives in `reuleauxcoder-tui/`. Its protocol client owns framing and reverse interactions; state reducers retain complete content and drafts; React renders cached visible rows. The frontend derives menu groups and primitive form fields from the backend catalog, and consumes command-owned panel trees. Slash input selects a top-level menu. F2 exposes session/plan/job/startup facts; F4 expands reasoning and full structured tool output. The launcher owns a stdio backend process, with `--backend` supporting an SSH subprocess. See its README for parity, controls and verification.

The default TUI workbench theme uses amber controls, sage activity and blue metadata. Wide terminals show a height-budgeted sidebar: attention, execution, plan and Git summaries precede session and activity details. Git facts come from the backend's `runtime.git` RPC every five seconds, using the existing bounded Git executor without consuming model-facing HEAD-change notices. F2 retains the full received snapshot; local upstream counts do not trigger network fetches. Approval actions wrap as whole items and paging information stays in the panel header.

TUI motion stays local to the affected React surface: startup and panel transitions brighten briefly without delaying input, and the composer rule animates during work then fades when idle. Approval controls remain stable; animation ticks do not modify controller state or transcript layout. Effects use existing theme colors and require no terminal shader.

The CLI is split by responsibility:

- `interfaces/entrypoint/cli.py`: local runtime ownership and cleanup; `interfaces/cli/main.py` dispatches stdio without loading terminal UI.
- `interfaces/cli/application.py`: connected-client view lifecycle, including host status over RPC.
- `interfaces/cli/repl.py`: JSON-RPC submission, interaction handoff and session lifecycle.
- `interfaces/cli/input.py`: prompt_toolkit line editing, runtime output pumping and draft preservation.
- `interfaces/cli/details.py`: restored conversation, session/execution facts and full tool output.
- `interfaces/cli/render.py`: append-only event routing and compatibility entry points.
- `history.py`: immutable history rows.
- `streaming.py`: assistant content streaming.
- `activity.py`: THINK/TOOL activity and live tool tail.
- `startup.py`: bounded startup/session plate.
- `prompt.py`: prompt_toolkit-native `YOU`/`CMD` input lane.
- `review.py`: shared framed approval/result review component.
- `theme.py`: Rich-only FORGE visual tokens.
- `views/builtin.py`: typed command view adapters.
- `interactor.py` and `interaction_presenter.py`: CLI interaction port implementation.
- `output.py`: serializes UI output onto the foreground terminal path.

Current CLI behavior:

- every CLI mode uses native terminal scrollback; there is no alternate screen or retained viewport;
- the interactive line prompt stays available while the backend runs; additional prompts and deferred commands use the runtime queues;
- reverse RPC interactions temporarily replace the prompt and restore its draft afterwards; worker output is drained on the foreground thread while the prompt is suspended;
- F2 / Ctrl+O prints session, execution, startup and queue details; F4 prints retained tool arguments and full output; `/thinking` displays full reasoning;
- Tab completes catalog-derived slash commands and Alt+Enter inserts a newline; the prompt distinguishes `YOU` and `CMD` input.
- write/edit approval previews share one framed diff renderer; additions/deletions use green/red backgrounds.
- an approved write/edit does not print the identical diff again after execution.
- if a file changes on disk while approval is pending, the preview is refreshed and approval is requested again.
- unsaved editor buffers are not visible to the CLI; editor-buffer integration requires a future editor adapter.
- Ctrl+C clears input, cancels approval, interrupts a running turn, or confirms idle exit. Ctrl+D and `/quit` also save and exit.
- assistant output renders Markdown, parsing complete blocks while streaming. Tool deltas append to scrollback during interactive input; one-shot and relay modes retain the transient activity renderer.

## Commands and interactions

`app/commands/service.py` owns command execution, capability checks, during-turn queues, auditing, session transitions, resume markers and exit snapshots. `app/rpc/server.py` owns chat workers, admission, interruption and interaction lifecycle; CLI/TUI adapters use `RuntimeClient` for both chat and commands. Slash input and typed `ActionRequest` submissions share this boundary. Session identity is read from the agent at execution time; frontends retain only revisioned backend snapshots.

The relay terminal adapter also uses a persistent RPC client/runtime per peer, while preserving the Go peer's HTTP transport. Bootstrap code owns backend objects; views consume initialization metadata and events. TypeScript's message peer and runtime client work without Node; stream framing and local file reads are separate Node adapters. View disposal does not own runtime shutdown. See `docs/frontend-runtime-boundary.md` for the boundaries, lifecycle and future desktop/VS Code Remote adaptation points.

Built-ins expose explicit `register_actions` and optional `command_panel_spec` contributions under `extensions/command/builtin/`. Each feature owns its parsers, parameter dataclasses, handlers, audit declarations and panel builders. The single `_BUILTIN_COMMAND_FEATURES` catalog pairs actions with panels. The service exposes a metadata-only `ActionCatalog` and immutable `PanelPresentation` data. Panel rows carry typed action requests; selection must not write slash text into the chat buffer. Frontends own cursor, filtering, focus, keyboard handling and framework-specific rendering.

`app/ui_events.py` and `app/interaction_contracts.py` own the shared output and request/response contracts. Frontends implement the interaction port; command features must not import interface modules. `infrastructure/rpc` provides bidirectional JSON-RPC 2.0 over complete-message transports. Local CLI/TUI use paired memory transports with real JSON serialization; `rcoder --rpc-stdio` runs the same backend across stdin/stdout. Protocol details and the VS Code Remote attachment model are in `references/reuleauxcoder-rpc-boundary.md`.

Command effects and UI view events derive their view type from the ViewModel. Pass the model to `open_view` or `refresh_view`; do not maintain a separate copy of its type in the request.

Scope labels in help:

- `[session]`: current runtime/session overlay.
- `[global]`: persisted workspace default.
- `[local-only]`: host-local capability.
- `[session-index]`: saved-session inventory/fingerprint operation.

Canonical session command surface:

- `/session`: list current-fingerprint sessions.
- `/session all`: list every fingerprint.
- `/session <#|id|latest>`: restore by displayed number, full ID, or newest current-fingerprint session.
- `/sessions` is a compatibility alias and is intentionally absent from primary help.
- `/save`: save the current session.
- `/new`: auto-save when configured, then create a clean session.

Other command families include `/help`, `/model`, `/mode`, `/approval`, `/skills`, `/mcp`, `/agents`, `/ps`, `/thinking`, `/tokens`, `/config`, `/debug`, `/compact`, `/reset`, and `/quit`. `/jobs` remains a compatibility alias for `/agents`; `/processes` and `/stop` are compatibility aliases for the `/ps` process family.

## Sessions

`domain/session/models.py`, `infrastructure/persistence/session_store.py`, and `app/runtime/session_state.py` own session persistence and restoration.

New sessions use a directory containing append-only `events.jsonl`, canonical `replay.json`, immutable `requests/`, `checkpoints/`, tool artifacts and a manifest; a lightweight JSON compatibility snapshot remains. Replay schema v3 includes wire-affecting request settings, exact hook-transformed provider payload hashes, and an aligned per-item ledger/checkpoint provenance vector that stays outside the provider payload. Resume preserves old base instructions and appends runtime/environment changes at the tail. Saved control state includes Plan/Progress revisions, actual usage observations and cache/checkpoint metadata.

`infrastructure/persistence/history_query.py` provides shared bounded message/event/artifact reads for model tools and `history.read/search/artifact` RPC methods. `events.jsonl` remains authoritative; a disposable per-session SQLite index consumes append batches and stores text chunks. Queries default to the active session, expose stable event/turn references, continuation cursors, indexing progress and recovery gaps. Summaries reuse exact replay provenance rather than matching truncated text. TUI F2 → h reads/searches history on demand; the live transcript lays out visible blocks with a 6,000-row LRU cache and stable reading anchors. Runtime ledger and live transcript content remain in memory. See `docs/history-query.md` for budgets and recovery semantics.

Session invariants:

- inventory is newest-first and fingerprint-scoped by default;
- previews use the latest meaningful user request and omit session lifecycle markers;
- numeric restore resolves against the current 20-entry fingerprint list;
- explicit IDs may cross fingerprints but emit a warning;
- interactive restore auto-saves the session being left when auto-save is enabled;
- the agent receives the full restored transcript;
- sessions created before the single-system context protocol take one explicit cache-epoch migration on first request; subsequent replay uses the current fixed system prompt and tagged synthetic context;
- the CLI replays only the latest three valid user turns and their assistant replies;
- `[SESSION_EXIT]`, `[SESSION_RESUME]`, tool messages and protocol-only entries do not pollute human replay.

## Approvals

`domain/approval.py`, `domain/approval_engine.py`, `domain/approval_preview.py`, and `app/runtime/approval.py` define approval requests, ordered rule evaluation, previews and stale-preview refresh.

Rules match tool name/source and resolve to `allow`, `warn`, `require_approval`, or `deny`. CLI, subagent and remote paths use one root-scoped `ApprovalCoordinator`; it serializes human focus without blocking request registration. Children inherit policy/provider but never inherit one-shot decisions. Optional auto-review requires an explicit reviewer profile, strict authorization evidence and fail-closed parsing. Mutating file tools carry a document snapshot so approval can detect intervening disk edits.

## Hooks and extensions

`domain/hooks/` is the typed hook runtime. Hook points cover tool/LLM execution and runner/session lifecycle. Guards authorize, transforms replace typed contexts, and observers receive immutable snapshots. Failures become structured diagnostics.

`domain/extensions/` adds versioned manifests, dependency ordering, runner/session/agent/subagent scopes, explicit subagent rebuild/omit policy, and reverse-order disposal. The legacy hook runtime is bridged through `app/runtime/extension_bridge.py`; new extension work should use explicit scope ownership rather than module globals or shallow copies.

Built-in hooks include tool policy, tool output truncation/archive, project context, LSP edit observation/diagnostic injection, and bounded Git-state injection. Git state is sampled by a root-local `BEFORE_LLM_REQUEST` transform and inserted only into the volatile execution overlay; it never mutates replay history, never runs against remote or child workspaces, and never blocks a model request. Status, changed HEAD commits and generic path-prefix summaries have strict time/output limits; non-repositories are reported explicitly.

## Subagents

`extensions/subagent/manager.py` owns the root-scoped asynchronous control plane: registration-before-submit, shared depth/concurrency limits, cumulative execution budgets, typed immediate-parent mailboxes, checkpoint resume, cancellation epochs, timeout, pruning and shutdown. Root tools are split into `spawn_agent`, `send_message`, `list_agents`, `wait_agent`, and `interrupt_agent`; spawn returns a job ID without waiting and the parent loop never implicitly waits. Child reports/progress are non-blocking; `request_guidance` checkpoints and parks the same job without occupying a worker slot. A valid directive resumes that job from an exact transcript prefix, including after process/session restore. Mailboxes persist queued/delivered watermarks and directives carry stable IDs. Execute completion is gated by an automatic verify job during the live runtime. Optional execute isolation uses retained git worktrees and requires explicit cleanup.

Child model loops run in isolated spawn processes. They own no workspace/LSP/remote primitives: scoped tool calls cross typed IPC to the parent Tool Broker, which reuses the normal authorization/approval/backend path. Read/list/glob/grep and the `lsp`, `lsp_status`, and `lsp_diagnostics` tools form the approval-free child baseline; write/edit/shell inherit parent policy and require a child reason. Children never receive agent lifecycle or Plan writer tools, so delegation is non-recursive. Worker envelopes carry session/worker generation, cancellation epoch, sequence and payload hash. Large broker results are archived content-addressably and sent as a verified model projection plus `ToolResultRef`; cancellation quarantines late results, and an effectful call without a committed outcome becomes human-visible `indeterminate` rather than being retried.

`domain/context/rounds.py`, `budget.py`, `checkpoint.py`, `usage.py`, `replay.py`, and `provider.py` define protocol-safe API-round boundaries, actual-first usage calibration, canonical replay, versioned replacement history, and the provider cache-compaction extension boundary. Automatic snip, semantic summary, and emergency collapse are independently configurable and enabled by default; model profiles may override individual switches while inheriting the remaining global policy, and forced manual strategies remain available. Main-model switches, session restore, and isolated subagent routing re-resolve the selected profile policy. At 60% request capacity, enabled deterministic provider/tool-output snipping is evaluated without mutation and commits only when it reclaims at least 20% of total capacity; at 75%, enabled snip and semantic summary are batched into one cache epoch targeting about 40%; 90% is emergency-only. Compression preserves tool-call/output adjacency and at least the five latest user turns. Partial and full-recovery summaries use a validated deterministic+LLM schema, bounded projection, independent output limits, and HistoryLedger provenance; legacy phase checkpoints remain readable. Checkpoints precede retained recent rounds and are persisted rather than regenerated on resume.

Subagents receive rebuilt scoped tools/hooks instead of sharing mutable instances. Approval delegation uses the shared provider path. LSP-consuming hooks are scope-aware so a child cannot drain or inject the parent's diagnostics. Child assistant/tool streams stay out of the root transcript; the Execution Panel still receives compact activity, current tool, budgets and blockers.

## LSP

`extensions/lsp/manager.py` owns the worker thread, workspace/language clients, document versions, generation watermarks and diagnostic batches. `client.py` owns JSON-RPC/LSP transport. `registry.py` owns language detection and server commands.

`documents.py` owns bounded stable file reads and handle cleanup. Subagent prompt
and result projection live in `extensions/subagent/result_projection.py`, whose
result input is a typed evidence snapshot; scheduling and transcript writes stay
with their runtime owners.

Key invariants:

- document versions increase monotonically;
- diagnostics replace/clear by URI and version rather than append forever;
- queued/completed work is scoped by agent, session generation, turn, tool call, file and workspace root;
- reset/restore invalidates stale generations;
- parent/subagent and multiple workspace roots do not consume each other's diagnostics;
- remote workspaces do not run host LSP against an unrelated local file view;
- push diagnostics, LSP 3.17 pull diagnostics and server-initiated requests are supported;
- shutdown and bounded respawn happen on the owning worker/event-loop path.

Successful edits enqueue document diagnostics and wait up to `lsp.edit_wait_timeout_ms` (default 1,000 ms) to append their own result to the tool's model projection. Inline results are consumed once and retained with tool history; late results fall back to request-time injection and dispatch acknowledgement. Both paths default to 20 diagnostics per file, 1,000 characters per message and 12,000 characters for the complete injection, with explicit omission counts. Full runtime diagnostics remain available to UI/history. See `docs/lsp-diagnostics.md` for limits and delivery semantics.

Default matrix: Python, TypeScript 7 native, TypeScript 6 legacy, JavaScript, YAML, Bash, Go, C, C++, and Rust. TypeScript mode is `auto | native | legacy`; native uses `tsc --lsp --stdio`, legacy uses `typescript-language-server`.

## Remote peer

The host remote-exec extension owns authentication, peer registry, relay protocol, artifact validation, cleanup and presentation. The Go peer owns CLI transport, heartbeat/poll/retry, workspace confinement, atomic filesystem primitives, process lifecycle and terminal size reporting.

Interactive remote chat streams host-rendered output and forwards approvals to the same host approval path. The peer should not duplicate model, command, tool-policy, diff, hook, LSP or presentation semantics.

## Configuration

Ordinary attachments use the session-bound `attachments.begin/append/complete/cancel` RPC and shared client `uploadAttachment` byte source (`attachFile` in the Node adapter). Files stay in the backend workspace at `.rcoder/attachments/<session-id>/<attachment-id>/<name>`, with a 64 MiB per-file limit and 256 KiB chunks streamed through temporary files. The returned relative path is for explicit draft/tool use; uploading does not submit chat, parse content or replace the image pipeline. See `docs/attachments.md` for the contract and cleanup boundaries.

Image inputs use `domain/images.py` references and `infrastructure/persistence/images.py` immutable variants, with a separately bounded original cache. CLI/TUI recognize pasted local image paths and insert numbered markers; `/attach` remains a fallback. Imports use session-generation-bound chunks. Per-profile `support_modal` defaults to `[text]`; adding `image` enables image input. Request projection hides images for text models without changing canonical history. `context.image_retention` defaults to `history`; `user_turn` shares ownership across steering/tool/goal continuations and expires on the next real user turn. `attachments.image` controls compression and the original cache. HTTP 413 recovery is finite and request-only; `image_payload_observed` records Base64 payload across attempts. See `docs/images.md` for behavior and deferred clipboard/peer imports.

Workspace config is `.rcoder/config.yaml`; user config is `~/.rcoder/config.yaml`. Runtime sections are `app`, `models`, `modes`, `approval`, `prompt`, `skills`, `mcp`, `context`, `attachments`, `session`, `goal`, `tool_output`, `shell`, `web`, `lsp`, `remote_exec`, `ui`, and `cli`.

`domain/goal.py` owns the single persisted session Goal. RuntimeServer admits automatic turns only after user/client work drains; CLI/TUI share `/goal` actions and snapshots over JSON-RPC. Goal changes use the existing ledger and runtime snapshot. Default token budget is unlimited; main, summary and goal-owned isolated-worker requests count input minus cached input plus output. Interrupt pauses the goal; restored active goals wait for frontend readiness. See `docs/goals.md` for controls, recovery and accounting limits.

LLM-wide defaults use `app.request_mode` and `app.responses`; a profile may override them with `models.profiles.<name>.request_mode` and `.responses`. Responses currently requires `state: local`; `cache.mode` is `implicit` by default and may be set to `explicit` for models that support explicit breakpoints. `/model` profile switching reconfigures the provider, request mode and Responses cache policy together.

Use `/config` to inspect effective values and their sources. Session overrides layer over config defaults and must not silently rewrite persisted global settings.

## Development rules

- Prefer typed dataclasses and ports over dict/string protocols.
- Keep framework-specific styling in interface adapters.
- Keep model-context retention separate from human presentation folding.
- Preserve event correlation and session generation across async work.
- Long-lived resources need explicit scope, cancellation and disposal.
- Snapshot saves acquire context before the writer lock. Plan/Progress mutations release their controller lock before persistence/event delivery; RPC steering admission releases its admission lock before snapshot waits. Steering application takes context before steering. Preserve these orders and the subprocess concurrency regressions in `tests/app/runtime/test_persistence_lock_order.py`; see `docs/runtime-locking.md`.
- Entrypoint startup-progress and session-notification callbacks log unexpected exceptions with their traceback and propagate them. Do not add diagnostic-delivery buffers or nested fallback sinks to keep startup successful; the CLI cleans up initialized resources before an unexpected startup failure escapes. KeyboardInterrupt remains normal user control.
- Slash commands also log unexpected dispatch, audit and effect-delivery failures with their traceback and propagate them to the calling interface. Session restore errors are handled by the session command; lifecycle callbacks and diagnostic recorders do not have fallback chains that turn failures into successful command results.
- Do not add a second tool implementation to the peer or another interface adapter.
- Use `rg` for search and `apply_patch` for hand edits.
- Preserve unrelated user changes in a dirty worktree.

## Verification

```bash
uv run ruff check .
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    uv run pytest -q
(cd reuleauxcoder-agent && go test ./...)
RCODER_RUN_LSP_INTEGRATION=1 uv run pytest -q tests/extensions/lsp/test_integration_smoke.py
```

The real LSP integration suite requires the configured language servers to be available. Do not encode a historical pass count as a permanent repository fact.

## Release rules

- Keep implementation and CI fixes in separate, focused commits. The final release commit must be `chore(release): prepare vX.Y.Z`; the annotated `vX.Y.Z` tag must point to that commit. Put fixes before the release commit when arranging the history.
- Update `pyproject.toml`, the root package version in `uv.lock`, the version snapshot in this file, `CHANGELOG.md`, and the wheel installation links in both READMEs together.
- Keep `uv.lock` on the official PyPI registry and artifact URLs. A version bump must not commit machine-specific mirror rewrites or refresh unrelated dependencies.
- Build the bundled TUI and wheel/sdist, run `scripts/check-distributions.py`, and run `scripts/smoke-install.py` outside the checkout before publishing. Keep release notes consistent with the changelog.
- Push the final release commit to `main` first, then wait for **all CI jobs on that exact commit** to pass, including Windows. Only then push its release tag. The tag-triggered Release workflow has its own Linux checks and does not gate itself on the separate main CI result.
- Use `.github/workflows/release.yml` to build and publish the wheel, sdist, VS Code VSIX with its compatible core, six platform/architecture peer binaries, peer `SHA256SUMS`, and the `linux/amd64` and `linux/arm64` GHCR host images. Keep the extension package/lock version aligned with the release; release notes come from the matching changelog section.
- For a user-requested withdrawal and republication, stop any old release workflow before removing the old GitHub Release and remote tag. Fix and validate the replacement, put its release `chore` last, and repeat the CI-before-tag sequence. Protect any necessary branch-history replacement with an explicit `--force-with-lease` expected SHA; preserve unrelated user work.
- Before reporting completion, verify release-workflow success, the published asset names and checksums, and both image architectures. Confirm the tag, GitHub Release target and image revision identify the same final release commit, and report the release link and any remaining worktree changes.

## Detailed references

- `references/reuleauxcoder-implementation-master-record.md`
- `references/reuleauxcoder-cli-tui-architecture-notes.md`
- `references/reuleauxcoder-subagent-lsp-handoff.md`
- `references/reuleauxcoder-extensions-hooks-peer-notes.md`
- `references/reuleauxcoder-pre-tui-definition-of-done.md`
