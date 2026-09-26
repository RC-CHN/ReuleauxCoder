# Reasoning, thinking, returned content and replay

This reference describes the shipped rcoder implementation. Providers and gateways may support different parameters. Identify the profile's provider/request mode and resolved inherited values first. Change the target profile instead of accidentally affecting subagent or approval models through `app`.

## Five separate mechanisms

| User objective | Settings | Actual effect |
| --- | --- | --- |
| Spend more or less model computation | `reasoning_effort`, `reasoning_effort_values`, `reasoning_effort_param` | Request parameters; accepted values depend on protocol, SDK and model |
| Toggle compatible-API thinking | `thinking_enabled` | Sends `thinking.type` when no nonempty effort is set; nullable boolean |
| Receive and preserve visible reasoning | `preserve_reasoning_content` | Controls text collection, streaming callbacks and `LLMResponse.reasoning_content` |
| Change human-facing display | `ui.reasoning_display` | `hidden`, `indicator` or `inline`; frontends also have folding rules |
| Choose replayed visible reasoning | `reasoning_replay_mode`, `backfill_reasoning_content_for_tool_calls`, `reasoning_replay_placeholder` | Sanitizes history's `reasoning_content`; native encrypted Responses replay is separate |

A request to return chain-of-thought must be interpreted in terms of content the provider actually supplies: visible reasoning, a summary or no text. rcoder cannot reconstruct hidden reasoning. A placeholder is not real reasoning, and inline display cannot make a provider return missing content.

## Request precedence

Ordinary requests and model configuration probes use the same builder:

1. A nonempty `reasoning_effort` is mapped through `reasoning_effort_values`, falling back to the original string. This branch suppresses the `thinking_enabled` request parameter, even when thinking is false.
2. With null/empty effort, a non-null `thinking_enabled` sends `extra_body: {thinking: {type: enabled|disabled}}`.
3. With neither specified, neither control is sent. This uses provider defaults, not necessarily disabled reasoning.

Use `reasoning_effort: null` to explicitly clear inherited effort before controlling thinking. Avoid empty strings as semantic switches. Setting both can obscure request intent, while thinking still affects local history sanitation; normally select the control supported by the protocol.

A null/empty effort map uses `{low: low, medium: medium, high: high}`. Custom maps may contain JSON values, including numbers, but local acceptance does not prove provider support. The default map is not a whitelist: unsupported labels such as `none`, `minimal` or `xhigh` are forwarded unchanged.

`reasoning_effort_param` defaults to `reasoning_effort` and exists only on profiles, as does `reasoning_effort_values`. It cannot replace reserved parameters such as model/messages/temperature. Renaming remains constrained by the SDK signature; it is not arbitrary extra-body injection.

## Protocol behavior

| Provider / request mode | Effort | Thinking | Returned content and replay |
| --- | --- | --- | --- |
| `openai-compatible` / `chat-completions` | Top-level SDK argument named by `reasoning_effort_param` | `extra_body.thinking.type` when effort is absent; gateway must support it | Collects `delta.reasoning_content`; replay sanitation below applies |
| `openai-compatible` / `responses` | Always converted to `reasoning: {effort: value}`; custom parameter name is ignored | Unsupported: an unsuppressed true or false is rejected during request construction; use null | Collects reasoning summary/text deltas; native reasoning/function-call output items are separately stored in `provider_data` and replayed |
| `anthropic` / `messages` | Current adapter does not translate effort, but it still suppresses the thinking branch | Only forwards `{type: enabled|disabled}`; temperature is omitted when thinking is present | Can collect thinking deltas, but complete native thinking/signature block replay is not implemented |

There are currently no Anthropic YAML inputs for `budget_tokens`, adaptive thinking or `output_config.effort`. Explain this limitation instead of inventing fields or treating an effort map as support. Ordinary Messages without thinking remains usable. Check actual model/gateway support and validate connectivity or tool rounds as needed.

Responses always sends `store=false`, full local history and `include: [reasoning.encrypted_content]`; it does not chain `previous_response_id`. State is currently `local` only. Cache mode is `implicit` or `explicit`; explicit mode needs endpoint support. These are not reasoning summary settings: no YAML field currently controls `reasoning.summary`.

Temperature and output budgets remain independent. Structural validation may pass when a specific model rejects temperature, cache extensions or encrypted items; only probing/real requests can expose that mismatch.

## Collection and storage

- `preserve_reasoning_content: true` (default) accumulates visible reasoning actually received, streams it and records it in responses/history. It does not request disclosure of additional hidden content.
- False disables new visible-reasoning accumulation and reasoning callbacks. It is not merely a disk-storage switch; use the UI setting when the user only wants less clutter.
- Editing the file does not retroactively erase history. With `thinking_enabled: true`, the sanitizer preserves existing reasoning even when preserve is false; this is not a privacy-erasure feature.
- Responses `provider_data` is independent of the visible-text switch. Turning preserve off does not stop collection/replay or storage of encrypted items. The UI does not display encrypted items as readable reasoning.
- `app.llm_debug_trace` helps diagnose received streams, not obtain more reasoning. Logs can contain requests, conversations and tools; avoid copying sensitive raw traces into chat and restore the requested diagnostic setting afterward.

## Exact visible-history sanitation

The following applies to local `reasoning_content`; the final wire adapter still determines what is transmitted.

| Setting/message | Behavior |
| --- | --- |
| Replay mode null or `none` | No forced backfill; does not mean no replay. Existing reasoning may remain on tool-call assistants and later assistants in the same user turn |
| `tool_calls` and preserve true | Backfills missing fields on tool-call assistants and subsequent assistants in that user turn |
| Backfill boolean true and preserve true | Enables the same behavior even with null/none replay mode |
| Preserve false and thinking not true | Removes visible reasoning fields and does not backfill |
| Thinking true | Skips ordinary non-tool assistant stripping; does not itself trigger missing-field backfill |
| Thinking not true, ordinary assistant before any tool call in this user turn | Removes its visible reasoning; this is not full-history reasoning replay |

Backfill only fills **absent fields**, not existing null or empty-string values. The default is `[PLACE_HOLDER]`; a null/empty configured placeholder falls back to it. Placeholders cannot repair missing signatures or encrypted state. Enable them only for a verified endpoint compatibility need, not as a universal recommendation.

A new user message resets the per-turn tool-call flag. Tool-call ID repair, tool-result adjacency checks and empty-assistant-body placeholders are separate mechanisms unrelated to effort.

## Diagnose by symptom

- Hide thinking in the UI: keep model controls and preserve unchanged; use `ui.reasoning_display: hidden`. Frontend expansion state can also affect presentation.
- Change speed/effort: verify protocol/model support and edit the target profile; displayed text is not a measure of computation.
- Thinking has no effect: check inherited nonempty effort, protocol, returned fields, preserve and UI. Responses uses effort, not a thinking boolean.
- Tool rounds fail on missing reasoning_content: inspect provider requirements, received deltas, preserve, original history and replay; do not globally backfill to hide incompatibility.
- Full Anthropic extended thinking: disclose missing budget/signature-replay support rather than inventing successful configuration.
- Avoid storing reasoning: distinguish new visible text, old history and encrypted Responses state. No single current switch erases or disables all three.
