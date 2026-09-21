# Changelog

## Unreleased

- Added `/shell` selection with executable names, paths and environment labels, including lazy discovery of shells inside a chosen Windows WSL distribution. New processes and model shell guidance follow the selection; existing processes stay unchanged, scoped children inherit an isolated preference, and `/shell auto` restores automatic selection.
- Reused unchanged composed terminal rows across frames, keyed by width and ordered clipped/transformed writes, avoiding repeated cell-grid construction and serialization without changing animation timing.
- Skipped ANSI style-difference allocation for adjacent characters with identical styles, preserving upstream transitions and closing sequences byte for byte.
- Reused bounded ANSI parsing and Unicode width caches across Ink frames, scoped to each terminal root. A version/hash-checked adapter keeps source runs and release bundles consistent, with byte-for-byte upstream renderer regression tests.
- Cached fitted terminal rows with entry and memory limits, avoiding repeated ANSI clipping for rows shared by adjacent frames while preserving style closures and Unicode widths.
- Removed TUI scroll easing and input debounce: wheel and keyboard navigation publish their full movement immediately. Reading history remains paused through output, resize and detail changes, with a new-output indicator and explicit Ctrl+End resume.
- Unified wheel/page navigation across TUI lists, approvals and content panels. Scrolling preserves selection; confirming an offscreen selection first reveals it. Arrow navigation stops at list boundaries.
- Added visual-line Up/Down editing with Unicode cell geometry and remembered columns. Empty-input arrows and Alt+Up/Down recall history, restoring the original draft, cursor and image attachments; form fields use Shift+Tab to go back.
- Separated TUI mouse-wheel reports from keyboard input so scrolling cannot replace a draft with input history. Added `--no-mouse` for native terminal selection and restored mouse modes on exit.
- Bounded tool-output projection memory by locating retained head/tail lines without splitting the whole result. Full-output archives now encode, write and hash bounded chunks in one pass.
- Replaced shell's 50 ms remote polling loop with cancellable long waits. Interrupting a wait keeps the process running and preserves unread output for the next poll.
- Broadcast peer output changes to all concurrent poll consumers, keeping process observation and tool waits responsive together.
- Pruned glob traversal to directories that can still match the pattern, in both local workspaces and remote peers.
- Paged file reads in local and remote workspace adapters, with bounded line retention, a 256 Ki-character page budget and explicit continuation instead of whole-file downloads. Older peers require an upgrade for paged reads; explicit full reads remain available.
- Reported incomplete directory scans even when filtering leaves no matches, instead of incorrectly claiming that matching files do not exist.

## 0.10.1 - 2026-09-20

- Returned fresh LSP diagnostics directly with successful file edits and writes, preserving the original tool output and retaining diagnostics in conversation history.
- Added a configurable one-second edit diagnostic wait, with event-driven wakeups, cancellation support and request-time fallback for late results without duplicate delivery.
- Shared bounded diagnostic projections across immediate and deferred delivery, with protocol tests for push/pull servers, timeout fallback and cancellation.

## 0.10.0 - 2026-09-17

- Added image inputs across Chat Completions, Responses and Anthropic Messages. Model profiles declare `support_modal: [text, image]`; omitted capabilities default to text only.
- Preserved immutable image references through history and model switches, with request-only placeholders for text models. Images follow effective history by default, with optional `user_turn` retention that shares ownership across steering, tools and automatic goal continuations.
- Added image path paste recognition and inline `[Image #N]` markers in the CLI and TUI, plus session-bound chunked uploads from local frontends to SSH backends. Native screenshot clipboard bridging and remote peer image imports remain deferred.
- Compressed ordinary images to 256 KiB and a 2000 px maximum edge by default, added bounded detail crops through `view_image`, and separated the 1 GiB per-session original cache from persistent sent variants. Finite HTTP 413 recovery and per-attempt Base64 accounting make image traffic visible.
- Bounded local regex search and moved remote searches to peer-side execution, with shared matching, output budgets and compatibility checks.
- Unified bounded LSP diagnostic projections, woke waiting readers on publication, and folded diagnostics behind TUI detail controls.
- Added shared proxy routing for web tools, a context compression strategy picker, and process browsing and controls inside TUI panels.
- Fixed TUI IME cursor positioning across independent frame updates, and wrapped and folded Markdown tables across history blocks.
- Repaired legacy session event sequences and prevented sequence reuse across store-side event creation.
- Preserved interrupts received after RPC admission but before chat or command execution starts, while clearing previous stop state when admitting the next operation.
- Staged freshly built release peers into Docker images instead of shipping older checked-in binaries, and recorded the tag commit explicitly in GitHub Release metadata.
- Received fragmented TUI RPC frames without repeatedly copying their prefixes, and maintained bounded streaming shell previews without rescanning complete output. Expanded tool records retain the full output.
- Serialized session manifests without copying separately stored replay artifacts, and reused committed message hashes for request and live-session provenance, rebuilding indexes after history replacement or restore.

## 0.9.3 - 2026-09-11

- Prioritized scroll frames over event batching, added immediate first-row feedback, and reused stable sidebar/text surfaces. Reproducible Ink benchmarks now cover continuous panel and transcript scrolling, input latency, idle RPC traffic, and multi-megabyte streaming replies.
- Shared the local animation scheduler across busy indicators, panel transitions and the startup logo, with theme-aware fading at the logo's disappearing edge. Animation cadence remains independent of backend events and performance reporting.
- Incrementally split the tail of streaming replies and reasoning, retained bounded visible-row caches, and avoided repeated panel wrapping and full-history activity scans.
- Suppressed unchanged runtime snapshots and Git redraws, added conditional snapshot reads for CLI/TUI clients, and reduced idle TUI polling while preserving event-driven updates.
- Fixed recursive Markdown link rendering that could terminate the TUI with a stack overflow, and prevented stale asynchronous panel responses from overwriting newer state.
- Isolated failure-recording errors so the original runtime failure remains available.

## 0.9.2 - 2026-09-11

- Added persistent session goals with shared CLI/TUI controls, model-facing create/read/complete tools, automatic continuation, user-input priority, pause/resume and recovery across restarts and compaction.
- Defaulted goal budgets to unlimited. Optional cumulative limits count input minus cached input plus output across main, summary and goal-owned subagent requests; estimated usage is labelled and budget exhaustion allows a final wrap-up.
- Unified bounded session-history search, message/event reads and artifact pagination over a rebuildable index, with stable source references preserved through compaction.
- Added paged history browsing/search in the TUI and bounded transcript layout caching for long sessions. The sidebar now displays goal status, objective and usage.
- Corrected Anthropic input accounting to include cache creation and reads before applying the shared cache deduction, and highlighted Node installation guidance when the launcher falls back to the CLI.

## 0.9.0 - 2026-09-10

- Migrated chat, slash commands and approvals to a shared bidirectional JSON-RPC runtime, with command-owned panels and typed actions for independent frontends.
- Added a React + Ink TUI with configurable semantic themes, responsive session/Git sidebar, guided approvals, compact tool groups, Markdown reasoning, visible queues and a collapsing startup header.
- Replaced the Python mini-TUI with a linear CLI using native terminal scrollback, while preserving command and interaction support through the same runtime.
- Bundled the TUI and its JavaScript dependencies into wheels and source distributions. `rcoder` selects the TUI when Node >=22 and a terminal are available, explains CLI fallback, and provides explicit `rcoder-cli` and `rcoder-tui` entry points without an npm install step.
- Made new sessions discoverable immediately and checkpointed response, reasoning and tool output for recovery after unexpected interruption. Restored partial output is marked as interrupted without automatically replaying work; the last pending batch and unsent drafts are not covered.
- Fixed stopping-state cleanup, output-follow scrolling and Windows CLI prompt cancellation coverage; added frontend, packaging and isolated-install CI checks.
- Removed retired adapters, duplicate command registration and redundant state, and made unexpected command and startup failures propagate with diagnostics.

## 0.8.2 - 2026-09-01

- Fixed Responses fallback replay to encode assistant text as `output_text`, preventing invalid `input_text` payloads and intermittent HTTP 400 failures after context reconstruction.

## 0.8.1 - 2026-08-27

- Defaulted Responses prompt caching to implicit mode for broader model compatibility while retaining explicit cache breakpoints as an opt-in policy.
- Fixed Responses stream lifecycle reporting so the TUI leaves its first-response waiting state as soon as a response starts.
- Added independently configurable automatic snip, semantic-summary, and emergency-collapse strategies with global defaults, per-model overrides, runtime switching, session restore, and subagent inheritance.
- Stabilized the cross-platform remote-process lifecycle integration test by waiting through the process deadline and relay grace period while preserving actionable terminal diagnostics.

## 0.8.0 - 2026-08-27

- Added a configurable OpenAI-compatible Responses request mode alongside the existing Chat Completions default, keeping canonical conversations local while rebuilding a stateless full request on every round.
- Added native encrypted-reasoning and tool-call replay, streamed text/reasoning/refusal/tool-call normalization, cached-input usage reporting, and request-mode-aware model profile switching.
- Added stable session prompt-cache keys and explicit or implicit cache policies that keep volatile execution overlays outside persisted history and after the stable cache boundary.
- Reduced the initial shell yield to five seconds and removed redundant copies and legacy mutation paths from the agent and workspace pipelines.

## 0.7.0 - 2026-08-12

- Added a provider-neutral model boundary and a native Anthropic Messages/SSE transport with typed tool, thinking, usage, protocol and transport handling.
- Reworked LSP lifecycle and diagnostics around per-transport concurrency, generation-safe state, bounded end-to-end deadlines, exact document synchronization, late diagnostic carry-forward, stderr/performance telemetry, and explicit `lsp_diagnostics` and scoped `lsp_restart` tools.
- Added generation-safe MCP runtime slots, connect/reconnect single-flight, truthful disconnect/EOF state, dynamic `tools/list_changed` reconciliation, atomic Agent catalog updates, refresh/renew telemetry, and bounded process-tree cleanup.
- Added bounded/coalescing TUI event delivery and generation-safe asynchronous transcript resize prewarming with cache and latency observations.
- Added a rebuildable SQLite session inventory projection with crash-window dirty markers, freshness validation, automatic corruption recovery, and fast aggregate queries while retaining ledger/replay files as authority.
- Preserved safe failure facts across hooks, tools, LSP, MCP, startup callbacks, session restore and UI observers so primary failures remain explicit and only secondary runtime-crashing failures are isolated.

## 0.6.3 - 2026-08-01

- Prevented startup and session-restore stalls by lazily initializing the tokenizer, reporting vocabulary-load progress, bounding tokenizer setup time, and falling back to a deterministic character/word estimate with conservative handling for long unbroken text when exact counting is unavailable.
- Added bounded runtime performance telemetry for startup, hooks, model calls, tools, MCP, context work and persistence, exposed through `/status perf`, alongside short-lived Git and project-context caches.
- Made archived tool-output recovery session-aware and character-paged, with exact continuation hints and no recursive truncation or re-archiving of `artifact_read` results.
- Sanitized terminal control characters before measuring TUI display width.

## 0.6.2 - 2026-07-29

- Added `web_fetch` and `web_search` network tools with warn-level approval and per-URL/query grants, a `web` config section, HTML-to-Markdown retrieval with size and timeout caps, and unified dual-provider search (Exa/Parallel) with per-call rotation, failover and optional API-key overrides.
- Hardened the web tools with bounded streamed responses, cancellable in-flight calls, credential-safe failure reporting, and strict redirect/provider parsing.

## 0.6.1 - 2026-07-29

- Serialized side-effecting tool calls and added revision-aware, verified workspace mutation receipts so local and remote edits report external changes, conflicts and uncertain outcomes instead of silently losing them.
- Applied queued user steering at interrupt boundaries with explicit per-call interruption policies, while relaying active-chat controls through remote peers.
- Made in-flight MCP cancellation prompt and quarantined late results without unsafe retries.
- Fixed the verified-edit regression coverage to preserve both LF and CRLF content consistently across Linux and Windows.

## 0.6.0 - 2026-07-28

- Added session-owned resumable shell processes with independent initial-yield and hard-runtime deadlines, plus `shell_session` polling, TTY input, soft interrupt and process-tree termination.
- Added bounded local PTY/ConPTY and remote process primitives, secure direct terminal input, request-time session inventory, live process UI events and `/ps` controls across user turns and context compaction.
- Preserved shell commands unchanged through structured shell argv while reporting schema, capability, policy and resource rejections before dispatch and ambiguous post-dispatch operations as explicit execution facts.
- Hardened process lifecycle behavior with bounded output and input, idempotent controls, monotonic remote state, concurrent approval-aware tool dispatch, capacity accounting and bounded shutdown/reap reporting.

## 0.5.1 - 2026-07-28

- Reorganized the production prompt_toolkit TUI into focused interface modules and moved TUI-only code out of the plain CLI package without changing user-facing behavior.
- Colocated typed interactive panel contributions with their command features while keeping selection, focus, refresh and canonical slash-command execution in a generic TUI host.
- Replaced import-time command, tool and hook registration with explicit ordered contributions and tightened runtime dependency injection and tool scope enforcement.
- Improved session persistence bounds, request retry behavior, cancellation recovery, remote action-registry reuse and retained transcript compatibility.

## 0.5.0 - 2026-07-21

- Reworked interactive command surfaces into panels: a registry-driven slash command popup, a modal selection panel piloted by /mode, two-level /model and /approval editors with rule deletion, toggle panels for /mcp and /skills, a /thinking effort picker, a /session picker with live text filtering, and an /agents jobs browser with per-job actions.
- Formatted every command view as aligned text with empty-state hints, retiring raw JSON dumps; the execution panel now shows the active model and context capacity.
- Added queued user steering above the input lane with a dedicated transcript event, turn pivoting on interrupt, and safe/stateful command policies during active turns.
- Persisted skills-disabled state and runtime state per session, initialized MCP concurrently, and lazy-loaded the subagent runtime.
- Improved performance through incremental artifact persistence, prompt/schema/hook caching, bounded stream queues with prompt cancellation abort, and startup/exit session scan avoidance.
- Fixed approval-time draft preservation with Y/N single-key handling, cancellation resets between operations, provider consumer cleanup, preflight rejection of invalid tool calls, CJK markdown width handling, and defaulted shell output filtering (rtk) to off.

## 0.4.4 - 2026-07-14

- Reworked automatic context compression into cache-preserving capacity tiers: deterministic snip commits from 60% only when it reclaims at least 20% of total request capacity, semantic summary starts at 75%, and 90% remains emergency-only.
- Decoupled progress reporting from compression, calibrated reclaim estimates from upstream usage, emitted compression lifecycle UI before slow work, and kept storage/UI token projections consistent.
- Preserved at least the five latest user turns across summaries while aligning the retained boundary to complete protocol rounds so tool calls and results remain adjacent.

## 0.4.3 - 2026-07-13

- Added bounded Git working-state and changed-HEAD summaries to the volatile request overlay, alongside stale-safe LSP diagnostics, without mutating replay history or sampling remote/child workspaces.
- Hardened global/workspace notes with durable scoped storage and editing, capped all active subagents globally at four, and tightened capacity and terminal-event behavior.
- Improved mini-TUI structure, transcript grouping, Markdown scroll anchoring and rendering performance through incremental retained state, LRU caching and plain-text fast paths.
- Made local and remote workspace search primitives faster and cross-platform while preserving result semantics, and allowed external read-only access with exact approval previews for external mutations.
- Added SOCKS proxy support and fixed the remote peer process race that could lose stdout/stderr from immediately exiting commands.

## 0.4.2 - 2026-07-13

- Hardened asynchronous subagent lifecycle handling with isolated workers, durable guidance parking/resume, stable mailbox ordering, parallel broker request queuing, terminal-result delivery receipts, partial handoffs at round limits, and strict root/child tool scopes.
- Improved the FORGE mini-TUI with a virtualized retained Markdown transcript, native terminal text selection, wheel/page scrolling, sticky tail-follow, resize-safe reflow, review-time scrolling, corrected transcript chronology, and `/new` canvas reset.
- Added compact live subagent activity, budget and delivery projections while keeping child tool chatter out of the root transcript and retiring terminal rows when a new subagent batch starts.
- Strengthened context and replay behavior with cache-aware dynamic execution state, actual usage observations, structured delegated final reports, untrusted-data boundaries, checkpointed resume, and stale-generation quarantine.
- Tightened cancellation, approval inheritance, effect uncertainty, workspace refresh, timeout accounting, progress phases, and provider request budgets across local and remote execution paths.

## 0.4.1 - 2026-07-12

- Added actual-first context budgeting, cache-aware rewrite planning, validated partial/phase/recovery summaries with ledger provenance, canonical replay envelopes including wire settings, append-only history, exact hook-transformed request audit artifacts and persisted compaction checkpoints.
- Added root-scoped approval coordination, explicit fail-closed auto-review profiles and inherited subagent policy without inherited one-shot decisions.
- Added crash-recoverable typed immediate-parent subagent mailboxes, audited parent directives, awaited/detached continuation, runtime-managed execute→verify barriers, user steering at safe boundaries, persisted job lifecycle/stale recovery and execute-result conflict detection.
- Added authoritative Plan/Progress control state and an ephemeral request overlay that stays out of conversation history while remaining auditable.
- Added the prompt_toolkit FORGE mini-TUI with a persistent execution panel, scrollable transcript, focused approval pane, real-event activity leases, resize reflow and deterministic Ctrl+C behavior.
- Made `/agents` the canonical subagent control surface; `/jobs` remains compatible.

## 0.4.0 - 2026-07-12

- Added typed runtime events, structured tool outcomes, a deterministic presentation reducer, typed command views, and shared interaction coordination.
- Migrated local and remote CLIs to the same presentation path and reduced the remote peer to transport, terminal, interaction, workspace, and process primitives.
- Added scoped extension lifecycles, isolated subagent tools and generations, and stale-safe LSP diagnostic routing.
- Added TypeScript 7 native LSP support while retaining an explicit TypeScript 6 legacy path.
- Added cross-language protocol fixtures, peer checksum verification, cross-platform release artifacts, and CI size/dependency gates.
- Consolidated effective configuration diagnostics and confined legacy model aliases to configuration migration.
