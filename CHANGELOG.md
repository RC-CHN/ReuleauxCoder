# Changelog

## Unreleased

## 0.4.1 - 2026-07-12

- Added actual-first context budgeting, cache-aware rewrite planning, deterministic+semantic summaries, canonical replay envelopes, append-only history, request audit artifacts and persisted compaction checkpoints.
- Added root-scoped approval coordination, explicit fail-closed auto-review profiles and inherited subagent policy without inherited one-shot decisions.
- Added typed immediate-parent subagent mailboxes, awaited/detached continuation, user steering at safe boundaries, persisted job lifecycle/stale recovery and execute-result conflict detection.
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
