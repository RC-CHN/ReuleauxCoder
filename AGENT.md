# ReuleauxCoder Project Context

This file describes the current repository, not a future design. Detailed design history and acceptance evidence live under `references/`.

## Current snapshot

- Package version: `0.6.2`.
- Primary shipped interface: prompt_toolkit-owned interactive mini-TUI; Rich remains the append-only renderer for one-shot, non-TTY, server and remote-peer paths.
- TUI status: the production CLI now owns a persistent viewport, but there is still no production Textual application.
- Remote peer: `reuleauxcoder-agent/`, a CLI-only Go peer.
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
├── interfaces/      # CLI adapter, interface ports, bootstrap
└── compat/          # compatibility helpers

reuleauxcoder-agent/
├── cmd/reuleauxcoder-agent/
└── internal/{client,process,protocol,runner,terminal,tools,workspace}/
```

Layer rules:

- Domain and presentation code must not import Rich, prompt_toolkit, Textual, or CLI view code.
- Commands return `CommandEffect`, typed view models, interaction requests, and state changes; they do not construct Rich objects.
- Tools use `WorkspacePort` and `ProcessPort` primitives. Platform-specific behavior belongs in local or remote adapters.
- CLI and future TUI are adapters over the same runtime events, presentation semantics, command effects, and interaction ports.

## Agent and runtime

`domain/agent/agent.py` owns conversation state, session generation, active mode, stop state, lifecycle coordination, hook scope, and subagent result injection.

`domain/agent/loop.py` owns the LLM/tool round loop, context compression checks, runtime-tail context, token accounting, tool-call adjacency, and max-round handling.

Provider requests use exactly one leading `system` message. Project context, summaries, resume/runtime updates, subagent data, diagnostics and the volatile execution state are application-generated synthetic `user` messages with reserved provenance tags documented by that fixed system prompt. The provider boundary fail-closes legacy or extension-injected later system messages into `<legacy_runtime_context>`. Only nested/standalone runtime-instruction regions receive runtime-control authority; file, tool, note, Git, LSP and delegated payloads remain untrusted data.

`domain/agent/tool_execution.py` is the shared tool pipeline:

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

## Workspace and process primitives

- `domain/workspace.py`: `WorkspacePort` and filesystem result types.
- `domain/process.py`: `ProcessPort`, process request/result, timeout/cancel and stream callbacks.
- `infrastructure/workspace/local.py`: confined local filesystem adapter.
- `infrastructure/process/local.py`: local subprocess adapter with concurrent stdout/stderr draining, cancellation, timeout, and partial-output preservation.
- `extensions/remote_exec/backend.py`: remote tool backend that forwards the same workspace/process primitives.

Product tools in `extensions/tools/builtin/` compose those primitives. The Go peer does not own a second product-tool policy layer:

- protocol v2 exposes workspace and process primitives;
- legacy protocol v1 accepts shell only and adapts it to the same process manager;
- read/write/edit/list behavior is driven by host tools through remote workspace operations;
- approval, display, diff policy, retention and LSP decisions remain host-owned.

Output retention is tool-directed through `ToolRetentionHint`: read uses head/anchor semantics, shell uses tail semantics, and search/list tools may use head-tail. Timeout/cancel outcomes keep partial output; the CLI shows a rolling five-line live tail while the agent retains the full result subject to context policy.

## CLI and presentation

The CLI is split by responsibility:

- `interfaces/cli/mini_tui.py`: prompt_toolkit viewport, bottom interaction focus and the shared-state adapter used for interactive TTYs.
- `interfaces/cli/markdown_fragments.py`: retained Rich Markdown-to-prompt_toolkit fragments with committed-stream boundaries and width/revision caching.
- `interfaces/cli/render.py`: event routing and compatibility entry points.
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

- interactive TTYs use a fixed top Execution Panel, virtualized transcript viewport and bottom input/review pane; F2 toggles startup/session details;
- prompt_toolkit exclusively owns cursor, focus, SIGWINCH resize and alternate-screen lifecycle; worker threads only update source-backed reducers and invalidate the app;
- non-TTY, `--prompt`, server and remote-peer paths remain append-only and never start the mini-TUI;
- the bottom `YOU` lane uses a high-contrast background for user input; the append-only compatibility prompt still distinguishes slash commands as `CMD`.
- write/edit approval previews share one framed diff renderer; additions/deletions use green/red backgrounds.
- an approved write/edit does not print the identical diff again after execution.
- if a file changes on disk while approval is pending, the preview is refreshed and approval is requested again.
- unsaved editor buffers are not visible to the CLI; editor-buffer integration requires a future editor adapter.
- panels and retained in-app transcript reflow on terminal resize. Ctrl+C clears input, cancels approval, requests a protocol-safe running-turn interrupt, or confirms exit according to focus.
- completed assistant cells render Markdown; streaming cells only parse committed blocks. Static cell/layout caches are keyed by revision and width, and the viewport no longer paints a transcript-height off-screen canvas on every frame.

## Commands and interactions

`app/commands/` contains the registry, parser, help generation, typed effects and shared view models. Built-ins are registered from `extensions/command/builtin/` with `@register_command_module`.

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

Other command families include `/help`, `/model`, `/mode`, `/approval`, `/skills`, `/mcp`, `/agents`, `/thinking`, `/tokens`, `/config`, `/debug`, `/compact`, `/reset`, and `/quit`. `/jobs` remains a compatibility alias for `/agents`.

## Sessions

`domain/session/models.py`, `infrastructure/persistence/session_store.py`, and `app/runtime/session_state.py` own session persistence and restoration.

New sessions use a directory containing append-only `events.jsonl`, canonical `replay.json`, immutable `requests/`, `checkpoints/`, tool artifacts and a manifest; a lightweight JSON compatibility snapshot remains. Replay schema v3 includes wire-affecting request settings, exact hook-transformed provider payload hashes, and an aligned per-item ledger/checkpoint provenance vector that stays outside the provider payload. Resume preserves old base instructions and appends runtime/environment changes at the tail. Saved control state includes Plan/Progress revisions, actual usage observations and cache/checkpoint metadata.

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

Child model loops run in isolated spawn processes. They own no workspace/LSP/remote primitives: scoped tool calls cross typed IPC to the parent Tool Broker, which reuses the normal authorization/approval/backend path. Read/list/glob/grep/query-LSP form the approval-free child baseline; write/edit/shell inherit parent policy and require a child reason. Children never receive agent lifecycle or Plan writer tools, so delegation is non-recursive. Worker envelopes carry session/worker generation, cancellation epoch, sequence and payload hash. Large broker results are archived content-addressably and sent as a verified model projection plus `ToolResultRef`; cancellation quarantines late results, and an effectful call without a committed outcome becomes human-visible `indeterminate` rather than being retried.

`domain/context/rounds.py`, `budget.py`, `checkpoint.py`, `usage.py`, `replay.py`, and `provider.py` define protocol-safe API-round boundaries, actual-first usage calibration, canonical replay, versioned replacement history, and the provider cache-compaction extension boundary. At 60% request capacity, deterministic provider/tool-output snipping is evaluated without mutation and commits only when it reclaims at least 20% of total capacity; at 75%, snip and semantic summary are batched into one cache epoch targeting about 40%; 90% is emergency-only. Compression preserves tool-call/output adjacency and at least the five latest user turns. Partial and full-recovery summaries use a validated deterministic+LLM schema, bounded projection, independent output limits, and HistoryLedger provenance; legacy phase checkpoints remain readable. Checkpoints precede retained recent rounds and are persisted rather than regenerated on resume.

Subagents receive rebuilt scoped tools/hooks instead of sharing mutable instances. Approval delegation uses the shared provider path. LSP-consuming hooks are scope-aware so a child cannot drain or inject the parent's diagnostics. Child assistant/tool streams stay out of the root transcript; the Execution Panel still receives compact activity, current tool, budgets and blockers.

## LSP

`extensions/lsp/manager.py` owns the worker thread, workspace/language clients, document versions, generation watermarks and diagnostic batches. `client.py` owns JSON-RPC/LSP transport. `registry.py` owns language detection and server commands.

Key invariants:

- document versions increase monotonically;
- diagnostics replace/clear by URI and version rather than append forever;
- queued/completed work is scoped by agent, session generation, turn, tool call, file and workspace root;
- reset/restore invalidates stale generations;
- parent/subagent and multiple workspace roots do not consume each other's diagnostics;
- remote workspaces do not run host LSP against an unrelated local file view;
- push diagnostics, LSP 3.17 pull diagnostics and server-initiated requests are supported;
- shutdown and bounded respawn happen on the owning worker/event-loop path.

Default matrix: Python, TypeScript 7 native, TypeScript 6 legacy, JavaScript, YAML, Bash, Go, C, C++, and Rust. TypeScript mode is `auto | native | legacy`; native uses `tsc --lsp --stdio`, legacy uses `typescript-language-server`.

## Remote peer

The host remote-exec extension owns authentication, peer registry, relay protocol, artifact validation, cleanup and presentation. The Go peer owns CLI transport, heartbeat/poll/retry, workspace confinement, atomic filesystem primitives, process lifecycle and terminal size reporting.

Interactive remote chat streams host-rendered output and forwards approvals to the same host approval path. The peer should not duplicate model, command, tool-policy, diff, hook, LSP or presentation semantics.

## Configuration

Workspace config is `.rcoder/config.yaml`; user config is `~/.rcoder/config.yaml`. Major sections are `models`, `modes`, `approval`, `prompt`, `skills`, `mcp`, `context`, `session`, `tool_output`, `lsp`, `remote_exec`, and `cli`.

Use `/config` to inspect effective values and their sources. Session overrides layer over config defaults and must not silently rewrite persisted global settings.

## Development rules

- Prefer typed dataclasses and ports over dict/string protocols.
- Keep framework-specific styling in interface adapters.
- Keep model-context retention separate from human presentation folding.
- Preserve event correlation and session generation across async work.
- Long-lived resources need explicit scope, cancellation and disposal.
- Do not add a second tool implementation to the peer or future TUI.
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

## Detailed references

- `references/reuleauxcoder-implementation-master-record.md`
- `references/reuleauxcoder-cli-tui-architecture-notes.md`
- `references/reuleauxcoder-subagent-lsp-handoff.md`
- `references/reuleauxcoder-extensions-hooks-peer-notes.md`
- `references/reuleauxcoder-pre-tui-definition-of-done.md`
