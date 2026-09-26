# config.yaml reference

This reference ships with the core and the self-contained `rcoder-config` skill. Follow [SKILL.md](../SKILL.md) to identify the core host, Python and configuration layers, then query `config describe` (or `config.describe` RPC) for the installed version. This document adds behavior and constraints beyond schema types. See [reasoning.md](reasoning.md) for reasoning semantics and [workflows.md](workflows.md) for executable checks and examples.

## Scope and loading

The public schema has **19 top-level sections and 133 field patterns**. Dynamic profile/server keys are represented by `{name}`, list elements by `[]`, and arbitrary-value dictionaries by one field. `meta` is an open object, not a single fixed key.

Precedence is builtin defaults → user `~/.rcoder/config.yaml` → workspace `.rcoder/config.yaml` → explicit `--config` file. Dictionaries merge recursively, including same-name profiles/modes/MCP servers; lists replace completely. Removing a workspace field reveals the lower layer, not a global disable. Null is not a universal reset/delete operation.

Paths belong to the core host. VS Code Remote uses the remote workspace/home. Relative paths are interpreted by their owning runtime components, not uniformly relative to the YAML file; prefer explicit host paths. There is no generic environment-variable interpolation: placeholder strings are not resolved credentials.

Persisted edits apply on the **next core start**. Session commands and restored sessions have separate runtime state. `inspect.sources` contains original layered data; `inspect.next_start` is a transformed internal Config and must not be written back wholesale as YAML.

Defaults below describe this implementation, not universal provider support. Verify real model IDs, capacity and supported parameters. `?` means nullable. Local validation proves structure only; provider and external-service behavior require separate checks.

## 1. models: profiles and model selection

Prefer explicit `models.profiles` with main/sub selections for new configuration. Every profile is validated, including inactive profiles. A placeholder key does not establish connectivity.

| Path | Type / default | Purpose and constraints |
| --- | --- | --- |
| `/models/active_main` | string? / first profile | Main profile; unknown references fail startup and checks |
| `/models/active_sub` | string? / main profile | Default subagent profile; does not change the main model |
| `/models/active` | string? / unset | Legacy selector, migrated only when active_main is absent |
| `/models/profiles/{name}/model` | string / `gpt-4o` | Nonempty provider model ID, not the local profile name |
| `/models/profiles/{name}/api_key` | string / empty | Every profile needs a nonempty key; inspect redacts it |
| `/models/profiles/{name}/provider` | string / `openai-compatible` | openai-compatible or anthropic |
| `/models/profiles/{name}/request_mode` | string? / `null` | chat-completions, responses or messages; inferred from provider when omitted |
| `/models/profiles/{name}/responses/state` | string / `local` | Only local state is supported; no server-side conversation chain |
| `/models/profiles/{name}/responses/cache/mode` | string / `implicit` | implicit or explicit; explicit needs endpoint support |
| `/models/profiles/{name}/support_modal` | string[] / `[text]` | Must contain text; may include image. Declaration cannot add vision to a text-only model |
| `/models/profiles/{name}/base_url` | string? / `null` | HTTP(S) endpoint; embedded auth, query and fragment are rejected |
| `/models/profiles/{name}/max_tokens` | integer / `4096` | Positive per-response output limit, not a cumulative Goal budget |
| `/models/profiles/{name}/temperature` | number / `0.0` | Local range [0, 2]; provider may be stricter |
| `/models/profiles/{name}/max_context_tokens` | integer / `128000` | Positive local capacity estimate; cannot expand actual model capacity |
| `/models/profiles/{name}/preserve_reasoning_content` | boolean / `true` | Collect, stream and preserve returned visible reasoning; independent of computation and native encrypted Responses items |
| `/models/profiles/{name}/backfill_reasoning_content_for_tool_calls` | boolean / `false` | Backfill missing reasoning fields for verified gateway compatibility |
| `/models/profiles/{name}/reasoning_effort` | string? / `null` | Effort label mapped/forwarded to provider; arbitrary strings are not guaranteed supported |
| `/models/profiles/{name}/thinking_enabled` | boolean? / `null` | Thinking request control; nonempty effort takes precedence |
| `/models/profiles/{name}/reasoning_replay_mode` | string? / `null` | null/none do not force backfill; tool_calls plus preserve backfills missing fields. See reasoning.md |
| `/models/profiles/{name}/reasoning_replay_placeholder` | string? / `null` | Backfill text; runtime fallback is [PLACE_HOLDER] |
| `/models/profiles/{name}/reasoning_effort_values` | object? / `null` | Label-to-JSON-value map; defaults to same-name low/medium/high |
| `/models/profiles/{name}/reasoning_effort_param` | string / `reasoning_effort` | Nonempty compatible-API argument name; cannot overwrite reserved fields; Responses uses its fixed effort mapping |
| `/models/profiles/{name}/context/auto_snip` | boolean? / `null` | Override global snipping; null/omission inherits |
| `/models/profiles/{name}/context/auto_summarize` | boolean? / `null` | Override global summarization |
| `/models/profiles/{name}/context/auto_collapse` | boolean? / `null` | Override global old-round collapse |

openai-compatible supports Chat Completions (default) and Responses; anthropic supports Messages only (default). Keep provider/request-mode pairs compatible. Static/startup checks cover all profiles; connectivity probes are selected separately. `model_targets` lists resolved targets and roles; a main-model success proves nothing about another profile. Probe failures do not prohibit saving.

## 2. app: shared model defaults and diagnostics

Model fields in app are shared defaults. File layers merge first, then app defaults merge into each profile, then type defaults fill remaining fields. Omitted profile fields inherit app; explicit null clears nullable inheritance (for example request mode returns to the provider default). Without profiles, legacy app configuration becomes a default profile. Startup, switching, subagents and checks use the same resolution.

| Path | Type / default behavior | Purpose |
| --- | --- | --- |
| `/app/model` | string / migrated `gpt-4o` | Legacy model ID |
| `/app/api_key` | string / migrated empty | Shared credential; redacted in inspect, plaintext in the source file |
| `/app/provider` | string / migrated `openai-compatible` | Legacy provider |
| `/app/base_url` | string? / `null` | Shared endpoint |
| `/app/max_tokens` | integer / migrated `4096` | Shared output limit |
| `/app/temperature` | number / migrated `0.0` | Shared temperature |
| `/app/max_context_tokens` | integer / migrated `128000` | Shared context capacity |
| `/app/request_mode` | string? / `null` | Inherited when omitted on a profile; explicit null infers by provider |
| `/app/responses/state` | `local` | Shared state policy; profile can override |
| `/app/responses/cache/mode` | `implicit` / `explicit` | Shared cache policy; nested fields merge |
| `/app/support_modal` | string[] / `[text]` | Shared modalities; profile list replaces it |
| `/app/preserve_reasoning_content` | boolean / `true` | Shared visible-reasoning collection/preservation |
| `/app/backfill_reasoning_content_for_tool_calls` | boolean / `false` | Shared backfill setting |
| `/app/reasoning_effort` | string? / `null` | Shared effort; explicit null clears inheritance |
| `/app/thinking_enabled` | boolean? / `null` | Shared thinking control |
| `/app/reasoning_replay_mode` | string? / `null` | Shared replay mode; values as for models |
| `/app/reasoning_replay_placeholder` | string? / `null` | Shared placeholder |
| `/app/llm_debug_trace` | boolean / `false` | Diagnostic LLM tracing; enable for a concrete need |

Edit a single profile when only that model should change. App changes affect all inheritors, including approval models. Inspect resolved model_targets instead of treating raw YAML as final request parameters.

## 3. context: model history and budgets

These settings affect model context. UI controls human display; tool_output controls tool-result retention. They are separate mechanisms.

| Path | Type / default | Purpose and constraints |
| --- | --- | --- |
| `/context/auto_snip` | boolean / `true` | Automatically trim old tool output; profile may override |
| `/context/auto_summarize` | boolean / `true` | Automatic summarization, potentially requiring extra model calls |
| `/context/auto_collapse` | boolean / `true` | Automatically collapse old rounds |
| `/context/image_retention` | string / `history` | history retains older images; user_turn limits to current user turn, including steering |
| `/context/snip_keep_recent_tools` | integer / `2` | Nonnegative protected agent-round count, not simply tool-call count |
| `/context/snip_threshold_chars` | integer / `1500` | Nonnegative character threshold |
| `/context/snip_min_lines` | integer / `6` | Nonnegative line threshold |
| `/context/summarize_keep_recent_turns` | integer / `5` | Nonnegative recent-turn protection |
| `/context/token_fudge_factor` | number / `1.1` | Positive token-estimate adjustment |
| `/context/reserved_output_tokens` | integer / `8192` | Nonnegative reserved output budget |
| `/context/fixed_prompt_tokens` | integer / `0` | Nonnegative fixed prompt reserve |
| `/context/tool_schema_tokens` | integer / `0` | Nonnegative schema reserve |
| `/context/safety_margin_tokens` | integer / `2048` | Nonnegative safety reserve |

The last four reserves reach the runtime budget. Larger reserves reduce conversation space; they do not enlarge the model.

## 4. ui: human-facing output

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/ui/verbosity` | string / `compact` | compact, standard or debug |
| `/ui/tool_output` | string / `summary` | errors, summary, preview or full |
| `/ui/max_preview_lines` | integer / `20` | Positive human preview line limit |
| `/ui/max_preview_chars` | integer / `1200` | Positive human preview character limit |
| `/ui/show_tool_args` | boolean / `true` | Display tool arguments |
| `/ui/reasoning_display` | string / `indicator` | hidden, indicator or inline; does not toggle model thinking |
| `/ui/notification_threshold` | string / `info` | debug, info, warning or error |

Frontends retain their own rendering rules. VS Code themes, fonts and extension installation paths are not YAML settings here.

## 5. tool_output: truncation and archives

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/tool_output/max_chars` | integer / `12000` | Positive retained-result character limit; affects model input |
| `/tool_output/max_lines` | integer / `120` | Positive retained-result line limit |
| `/tool_output/store_full_output` | boolean / `true` | Archive full truncated results for later reads |
| `/tool_output/store_dir` | string? / `null` | Defaults to .rcoder/tool-outputs with user fallback; session archives also relate to session storage |

Tool retention policy may preserve the head, tail or both. Smaller human previews do not establish lower model token usage.

## 6. attachments.image: image processing

All fields are positive integers. Ordinary non-image upload limits are not configured here.

| Path | Default | Purpose |
| --- | --- | --- |
| `/attachments/image/max_edge_px` | `2000` | Maximum image long edge in pixels |
| `/attachments/image/normal_max_bytes` | `262144` (256 KiB) | Normal image variant byte budget |
| `/attachments/image/detail_max_base64_bytes` | `2097152` (2 MiB) | Detail variant Base64 budget |
| `/attachments/image/originals_cache_max_bytes` | `1073741824` (1 GiB) | Original-image cache budget |
| `/attachments/image/import_max_bytes` | `67108864` (64 MiB) | Per-image import byte limit |
| `/attachments/image/max_pixels` | `40000000` | Decoded pixel limit |

Model image use also depends on support_modal and image_retention.

## 7. skills: discovery and activation

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/skills/enabled` | boolean / `true` | Overall skill/catalog switch |
| `/skills/scan_project` | boolean / `true` | Scan workspace .rcoder/skills/*/SKILL.md |
| `/skills/scan_user` | boolean / `true` | Scan core-host ~/.rcoder/skills/*/SKILL.md |
| `/skills/disabled` | string[] / `[]` | Disable resolved skill names; whole-list replacement |

Discovery order is builtin → user → workspace, with later same-name skills overriding earlier ones. Builtins update with the core and are not copied over user files. Disabling project/user scans does not disable builtins; use disabled names or the overall enabled switch.

There is no skills.paths, remote URL, marketplace or configurable builtin directory. SKILL.md requires YAML name and description; bodies load on demand. agents/openai.yaml is not a parser input. Installation directories and discovery hints both use .rcoder/skills/.

## 8. prompt: additional user instructions

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/prompt/system_append` | string / `""` | Adds user/workspace instructions through prompt assembly; does not replace the core prompt |

Change only the requested behavior. Do not turn the configuration task into permanent instructions. Project files, skill bodies and this field are distinct mechanisms.

## 9. mcp: local stdio servers

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/mcp/servers/{name}/command` | string / `""` | Executable name/path, not a shell command string; required when enabled |
| `/mcp/servers/{name}/args` | string[] / `[]` | Separate argv entries; inspect redacts the whole value |
| `/mcp/servers/{name}/env` | string-to-string object / `{}` | Process environment overrides; redacted by inspect |
| `/mcp/servers/{name}/cwd` | string? / `null` | Child working directory; prefer an absolute host path |
| `/mcp/servers/{name}/enabled` | boolean / `true` | Enable this server |

No url, transport, headers or SSE/HTTP server fields are supported. Do not copy another client's mcpServers layout. The client locates the executable, merges environment and starts stdio. Config validation does not install dependencies, start servers or complete handshakes.

## 10. lsp: language servers and diagnostics

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/lsp/enabled` | boolean / `true` | Overall LSP switch |
| `/lsp/poll_timeout_ms` | integer / `5000` | Positive request/diagnostic polling timeout, milliseconds |
| `/lsp/edit_wait_timeout_ms` | integer / `1000` | Nonnegative edit wait; zero consumes only ready results |
| `/lsp/max_diagnostics` | integer / `20` | Positive per-projection diagnostic limit |
| `/lsp/max_injection_chars` | integer / `12000` | Total injection character budget, at least 512 |
| `/lsp/max_message_chars` | integer / `1000` | Positive per-message character limit |
| `/lsp/include_warnings` | boolean / `true` | Include warning diagnostics |
| `/lsp/typescript_mode` | string / `auto` | auto, native or legacy; TS7 or traditional language-server route |
| `/lsp/servers/{name}/cmd` | string? / `null` | Override builtin language executable |
| `/lsp/servers/{name}/args` | string[]? / `null` | Override argv; null inherits, empty list clears |
| `/lsp/servers/{name}/workspace_root` | string? / `null` | Override root detection |
| `/lsp/servers/{name}/init_opts` | object? / `null` | Server-specific initialization options |

Supported names are python, rust, go, typescript, javascript, c, cpp, bash and yaml. Unknown languages fail static checks. Validation does not establish installation/initialization, and init_opts is not a complete per-vendor schema.

## 11. web: search and fetch

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/web/enabled` | boolean / `true` | Register web tools |
| `/web/proxy` | string / `env` | env, direct or HTTP/HTTPS/SOCKS5/SOCKS5H proxy URL; not a global LLM/MCP proxy |
| `/web/search_provider` | string / `auto` | auto, exa or parallel |
| `/web/allow_private_networks` | boolean / `true` | Permit private-network destinations |

Search keys come from EXA_API_KEY/PARALLEL_API_KEY environment variables; there is no web.api_key. Proxy URLs may contain credentials and are redacted in inspect. Raw files can still contain secrets.

## 12. shell: RTK hints

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/shell/rtk` | string / `off` | auto, on or off; controls RTK availability/install hints, **not automatic command rewriting** |

Shell selection uses the existing /shell session command. There are no executable, args or timeout fields here; remote timeouts belong to remote_exec.

## 13. session, 14. cli, 15. goal

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/session/auto_save` | boolean / `true` | Session auto-save policy, not a universal persistence disable/erase switch |
| `/session/dir` | string? / `null` | Defaults to workspace .rcoder/sessions, with user fallback if creation fails |
| `/cli/history_file` | string? / `null` | Linear CLI input history, default .rcoder/history; separate from model conversation history |
| `/goal/default_token_budget` | integer? / `null` | Positive cumulative default or null for unlimited; counts input minus cached input plus output |

Some paths expand tilde, including CLI history and some archives; other entry points do not. Do not promise that every directory field supports tilde. Explicit host paths are clearer.

## 16. modes: exposed tools and mode instructions

Edit through ordinary file tools and their approvals. Tool exposure and permission to invoke a tool are separate steps.

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/modes/active` | string? / `coder` | Must reference an existing mode; builtin coder/planner/debugger merge first |
| `/modes/profiles/{name}/description` | string / `""` | Mode description |
| `/modes/profiles/{name}/tools` | string[] / `[]` | Exposed tools; builtin coder uses ["*"]. Same-name builtin modes inherit fields before overrides |
| `/modes/profiles/{name}/prompt_append` | string / `""` | Additional mode instructions |
| `/modes/profiles/{name}/allowed_subagent_modes` | string[] / `[]` | Delegatable modes, builtin explore/execute/verify; distinct from main-mode profiles |

Legacy bash tool names migrate to shell in modes and approval rules. Use shell in new configuration.

## 17. approval: tool invocation policy

Edit through files. Default mode is require_approval, but omitted rules include builtin defaults; not every tool necessarily prompts. Explicit lists replace builtin/lower-layer lists, including an empty list.

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/approval/default_mode` | string / `require_approval` | allow, warn, require_approval or deny when unmatched; internal read/control tools have an allow fallback |
| `/approval/reviewer` | string / `user` | user or auto_review |
| `/approval/auto_review_model_profile` | string? / `null` | auto_review requires a valid explicit profile; no silent main/sub fallback |
| `/approval/auto_review_policy` | string / `""` | Additional policy for the approval model |
| `/approval/auto_review_timeout_seconds` | integer / `15` | Positive automatic-review timeout in seconds |
| `/approval/rules/[]/tool_name` | string? / `null` | Exact tool name |
| `/approval/rules/[]/tool_source` | string? / `null` | Source: builtin, mcp or unknown at runtime |
| `/approval/rules/[]/mcp_server` | string? / `null` | MCP server name |
| `/approval/rules/[]/effect_class` | string? / `null` | Tool effect category |
| `/approval/rules/[]/profile` | string? / `null` | Runtime approval-context profile dimension; do not assume a model ID |
| `/approval/rules/[]/pattern` | string? / `null` | Stable resource identifier: *, directory/** or exact value; not general glob/regex |
| `/approval/rules/[]/scope_key` | string? / `null` | Exact runtime scope key |
| `/approval/rules/[]/action` | string / `require_approval` | Same four actions as default_mode |

Rules sort by specificity, preserving list order for ties. Resources match separately, then the strictest action wins. Do not describe this as simple first-match list evaluation. Inspect resolved defaults before replacing rules.

## 18. remote_exec: rcoder relay

These are rcoder's own relay settings, not VS Code Remote SSH installation/connection settings. Edit through ordinary files.

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/remote_exec/enabled` | boolean / `false` | Enable relay capabilities |
| `/remote_exec/host_mode` | boolean / `false` | Run the core as relay host; --server also enables this |
| `/remote_exec/relay_bind` | string / `127.0.0.1:8765` | Listener address |
| `/remote_exec/bootstrap_access_secret` | string / `""` | Bootstrap secret, redacted by inspect |
| `/remote_exec/bootstrap_token_ttl_sec` | integer / `300` | Bootstrap token lifetime, seconds |
| `/remote_exec/peer_token_ttl_sec` | integer / `3600` | Peer token lifetime, seconds |
| `/remote_exec/heartbeat_interval_sec` | integer / `10` | Heartbeat interval, seconds |
| `/remote_exec/heartbeat_timeout_sec` | integer / `30` | Lost-heartbeat timeout, seconds |
| `/remote_exec/default_tool_timeout_sec` | integer / `30` | Ordinary remote-tool timeout, seconds |
| `/remote_exec/shell_timeout_sec` | integer / `120` | Remote-shell timeout, seconds |

Bind addresses use host:port or [IPv6]:port with ports 0–65535; zero allows OS allocation. TTLs, heartbeat and tool timeouts must be positive; heartbeat timeout must exceed its interval. Static validity does not prove the listener can bind or peers can connect.

## 19. meta: program metadata

| Path | Type / default | Purpose |
| --- | --- | --- |
| `/meta` | object / `{}` | Open JSON metadata object; no fixed nested field set |

meta.example marks example templates. Legacy meta.workspace_bootstrapped remains accepted but no longer triggers backfilling. If all existing configurations remain marked as examples, ordinary startup asks for setup. Do not remove an unfinished-setup marker merely to make a check green.

## Not part of config.yaml

- Internal notes_workspace_max, notes_global_max and notes_inject have defaults/consumers but no public YAML schema inputs; do not invent notes.*.
- There are no generic top-level hooks, plugins, subagents, retry, timeout, skills.paths or env sections. Startup and checks reject unknown fields.
- Ordinary upload limits, VS Code extension settings and terminal shortcuts/themes do not become YAML fields simply because internal constants exist.
- MCP HTTP/SSE and arbitrary model extra_body/headers are not exposed by this configuration.

## File-edit workflow

1. Query describe for the installed schema and inspect for source files, explicit launch layers and effective values.
2. Use workspace settings for a project and core-host user settings for cross-project defaults. Explicit files win; removing an override restores inheritance.
3. Read/edit original YAML with ordinary tools, preserving comments and concurrent changes. Unsaved editor documents can be checked without candidate files.
4. Run config check with the same core/workspace. Distinguish structural validity from model availability. Only request model probes when needed.
5. Report scope and next-start activation, then restart at an authorized time. There is no general hot reload; skills reload is unrelated.

The API only describes, inspects and checks. There are no special LLM writers, field permission branches, candidate state machines or validation leases. Existing file/shell permissions still apply. Do not expose credentials or save redaction placeholders.

## Validation boundaries

- API v2 advertises buffer_checks and profile_probes; old write operations are removed.
- Startup, inspect and check share strict parsing/merge validation; reads create no examples or backfilled fields.
- Static checks cover types, ranges, references, languages, relay addresses/timeouts and replay modes, not external availability.
- Startup checks construct all provider clients in an isolated process without starting an Agent, resuming a Goal, running hooks or starting MCP/LSP.
- Model probes use actual profile request parameters, at most 20 seconds each and 1–8 selected profiles, consuming provider tokens. Failed/unknown probes do not necessarily invalidate YAML or block saving.
- Connectivity checks do not validate tools, images, MCP/LSP, hooks or every task. Results are not cached as permission for future edits.
- VS Code starts with workspace settings, folds global defaults and explains explicit-file precedence. Native editors own writes; buffer changes invalidate displayed checks.
- Restart preflights saved settings. Unsaved/invalid configuration or running work blocks restart and preserves the current core. Native undo/file timelines may help recovery but do not guarantee history for external edits.
- Private backups from earlier development versions remain untouched; the current interface does not create or manage candidate/backup state.
