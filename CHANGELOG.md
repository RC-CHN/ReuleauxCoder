# Changelog

## Unreleased

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
