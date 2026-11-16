# Context compaction

Automatic and manual compaction use the same prefix-checkpoint policy. Ordinary
requests retain their history unchanged. Oversized new tool output is limited
and archived by the existing tool-output hook before entering model history;
automatic compaction does not repeatedly snip older outputs.

## Pressure and retention

Pressure is measured after request transforms and before dispatch effects. The
estimate includes system instructions, project context, tool schemas, history,
images and runtime context. Provider prompt usage calibrates subsequent local
estimates. Summary usage is accounted separately and never becomes the baseline
for a normal conversation request.

For model window `W`, output reservation `O` (the larger of the effective request
output cap and `reserved_output_tokens`) and safety margin `H`, the threshold is
`min(W * threshold_ratio, W - O - H)`. The default threshold ratio is `0.8`.
The newest complete API rounds retain at least `(W - O) * retain_ratio` estimated
tokens, with `retain_ratio: 0.16` by default. A single user task can therefore
compact after many tool rounds without needing additional user messages.

The oldest complete prefix is selected without separating a tool call from its
results. The summary request must also fit its own input/output budget. System
instructions and tool schemas cannot be reduced by history compaction. If the
fixed request overhead or an indivisible recent round prevents useful reduction,
the attempt reports that limitation rather than deleting additional history.

## Summary requests and commit

The auxiliary request reuses the prepared conversation system prompt, stable
project context, tool definitions and selected original prefix. It appends a
runtime compaction instruction instead of replacing the system prompt or
serializing the history into a new JSON prompt. The same model, session identity
and image-retention policy apply. Responses mode retains its session cache key
and places the new instruction outside the stable cache breakpoint. Actual cache
hits depend on the provider and cache lifetime; usage records include DeepSeek
cache-hit tokens when reported.

The summary is Markdown with fixed sections for intent, constraints, code,
progress and verification, errors, pending work and next action. Existing
checkpoints, including older JSON summaries, remain in the input and must be
merged with subsequent work. The model cannot execute tools during this call.
Source event IDs and checkpoint IDs remain machine-owned checkpoint metadata.

Empty, incomplete, malformed, tool-calling or non-shrinking summaries are not
committed. The complete replacement request is measured again. Automatic work
must fall below the trigger threshold; by default one additional summary attempt
is allowed if the first valid reduction is insufficient. Intermediate summaries
remain private. Cancellation, changed history, failed summaries or unsuccessful
convergence preserve the original model history and cache epoch.

A successful transaction commits one checkpoint and one cache epoch. Full human
history and tool artifacts remain available through the existing history tools.
Checkpoint metadata records summary input/output/cache usage and reclaimed
tokens. The next normal request may need to establish a cache for the new prefix.

## Manual operation and overflow recovery

`/compact` requests one useful checkpoint even below the automatic threshold and
reports when no complete prefix is eligible. It uses the same retention and
summary validation. There is no strategy picker or destructive deep-collapse
branch.

An explicitly classified provider context-window failure can request a more
aggressive prefix selection through the same transaction, retaining at least the
latest complete round. The original request is retried only after a successful
replacement, at most `max_overflow_retries` times (default `1`). This is separate
from ordinary provider transient-error retries. An unrecoverable overflow is
reported; invalid requests are not retried indefinitely.

## Configuration and migration

```yaml
context:
  auto_compact: true
  threshold_ratio: 0.8
  retain_ratio: 0.16
  summary_max_tokens: 8192
  compaction_retries: 1
  max_overflow_retries: 1
  reserved_output_tokens: 8192
  safety_margin_tokens: 2048
```

`summary_max_tokens` is the summary output allowance, including any reasoning
tokens charged against that cap. Total task token budgets also constrain summary
requests. Set `auto_compact: false` to disable automatic pressure and overflow
compaction while retaining `/compact`. A model profile may override
`models.profiles.<name>.context.auto_compact`; omission inherits the global value.

Remove `auto_snip`, `auto_summarize`, `auto_collapse`, `snip_keep_recent_tools`,
`snip_threshold_chars`, `snip_min_lines` and `summarize_keep_recent_turns` from old
configuration. They now produce an explicit migration error. Replace the old
automatic switches with `auto_compact`; choose `retain_ratio` for token-based
retention. Use `tool_output` settings for oversized new results. Replace
`/compact force <strategy>` with `/compact`. Existing persisted sessions and
checkpoints still load without regenerating or deleting historical records.
