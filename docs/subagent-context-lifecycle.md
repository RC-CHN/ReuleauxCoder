# Subagent and Context Lifecycle

This document records the v0.4.1 control-plane invariants shared by CLI and
future TUI frontends.

## Asynchronous ordering

```text
parent tool call
  -> validate depth/budget/context/isolation
  -> register job + cancel/message queues
  -> submit worker
  -> worker marks running
  -> child consumes parent projection
  -> child persists its complete transcript
  -> manager atomically records terminal status/result
  -> manager enqueues the job ID in the parent mailbox
  -> parent drains the mailbox at a tool-protocol-safe boundary
```

Worker callbacks never append directly to parent history. A running child only
consumes inter-agent messages before a new model round, never inside an active
tool batch. Session generation and parent identity are checked before delivery;
late completions remain inspectable but are not injected.

This follows two useful reference patterns without copying their UI/runtime:

- Codex keeps a root-scoped control plane with bounded execution, explicit
  wait/resume/message/close operations, and fork history modes.
- Claude Code registers background state before execution, persists transcripts,
  emits partial results on cancellation, and marks terminal state before doing
  optional notification embellishment.

## Context projection

- `minimal`: the two latest user messages.
- `recent`: the newest complete API rounds (default).
- `full`: the complete parent model history, explicitly requested.

Every child has independent tools, hooks, approval context and history. Nested
agents share the root control plane and are rejected above the configured depth.

## Results and resume

The model receives a structured result containing status, summary, evidence,
files, changes, unresolved work, usage and a transcript reference. Full child
history stays in `.rcoder/subagents/<job>.json`; follow-up invocations restore
that transcript instead of starting from a raw result string.

Execute agents may request detached git-worktree isolation. Worktrees are
retained for inspection/integration and explicitly removed with
`/jobs cleanup <id>`; core does not silently merge or delete changes.

## Context compaction

Compression uses the effective input budget after output, prompt, schema and
safety reservations. Every transform operates on complete API rounds, repairs
tool-call/output adjacency, and creates a versioned replacement checkpoint.

The order is provider cache compaction (when an adapter exists), old tool-result
snipping, partial summary, checkpoint summary, then emergency collapse. Provider
cache APIs are behind `ProviderContextCompactor`; no provider-specific request
shape leaks into core policy.
