# Changelog

## Unreleased

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
