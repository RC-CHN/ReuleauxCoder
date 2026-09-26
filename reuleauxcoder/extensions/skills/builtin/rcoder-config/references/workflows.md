# Configuration workflows and troubleshooting

## Check the same configuration

Use the Python, workspace and optional explicit file from the catalog's `<skill_runtime>`. Quote each argument for the actual shell: POSIX can invoke a quoted executable path; PowerShell uses `& 'C:\path with spaces\python.exe'`. Do not execute an argv JSON display as a shell string.

```text
<core-python> -m reuleauxcoder config describe --workspace <workspace>
<core-python> -m reuleauxcoder config describe --section models --workspace <workspace>
<core-python> -m reuleauxcoder config inspect --workspace <workspace>
<core-python> -m reuleauxcoder config check --workspace <workspace>
```

Append `--config <explicit-file>` to every command when one was supplied at launch. Keep the workspace stable even after changing the shell's directory. Without metadata, establish the real launcher/interpreter rather than relying on a stale PATH installation.

- describe works even with invalid configuration and returns API version, schema, capabilities and constraints.
- inspect returns sources, revision, valid, diagnostics, next_start and model_targets. The standalone command's runtime is null; it has not inspected current session overrides.
- Default check runs static + startup. Startup constructs provider clients in an isolated process without starting Agent/MCP/LSP. valid reflects static validity; inspect each checks[].status as well.
- `check --content - --scope workspace` checks a full raw YAML buffer from stdin without writing. Use user for global scope; explicit requires binding an explicit file. Content is a complete document, not a patch or JSON wrapper. RPC config.check accepts `documents: [{scope, content}, ...]`, at most three documents of 1 MiB each.
- `check --revision <inspect.revision>` detects changed sources, but cannot replace write-time conflict handling. Reread and merge your own edits instead of overwriting newer content.
- Only `check --check model --profile <name>` calls a provider. Repeat profiles up to eight; each probe has a 20-second limit. Preserve the reported failure stage and distinguish parsing, client construction and network/authentication errors.

## Examples and inheritance

These are **partial configuration snippets**, to merge into the target rather than replacing it. Model IDs and credentials below are offline examples. Use the user's real endpoint, key, model and capacity for actual requests.

<!-- example: base -->
```yaml
models:
  active_main: main
  active_sub: main
  profiles:
    main:
      provider: openai-compatible
      request_mode: chat-completions
      model: example-model
      api_key: example-key-not-a-real-credential
      max_context_tokens: 128000
```

Dictionaries merge recursively; lists replace entirely. A workspace can override a profile's effort without copying a user-layer credential. Profiles first inherit app defaults. Removing a workspace field reveals a user value before a builtin default; removing a workspace profile does not remove the same-name user profile.

### Hide reasoning in the human interface

<!-- example: hidden-ui -->
```yaml
ui:
  reasoning_display: hidden
```

Keep model thinking, effort and preserve unchanged. To disable model reasoning, verify the provider's supported control instead of substituting this UI setting.

### Increase effort for one profile

After verifying that the endpoint accepts high:

<!-- example: effort -->
```yaml
models:
  profiles:
    main:
      reasoning_effort: high
      thinking_enabled: null
```

Do not change app unless all inheritors should change. Use custom label mappings only when the endpoint supports the mapped API values; UI labels can differ from wire values.

### Compatible thinking and tool-round reasoning fields

Use only with a verified Chat Completions endpoint that accepts this format. Clear inherited effort so it does not suppress thinking. Placeholder replay cannot repair missing real signatures or reasoning state.

<!-- example: thinking-replay -->
```yaml
models:
  profiles:
    main:
      reasoning_effort: null
      thinking_enabled: true
      preserve_reasoning_content: true
      reasoning_replay_mode: tool_calls
      reasoning_replay_placeholder: "[PLACE_HOLDER]"
```

### Switch to Responses

The endpoint/model must support the protocol and rcoder's cache/encrypted-item request fields. Do not retain an inherited thinking boolean.

<!-- example: responses -->
```yaml
models:
  profiles:
    main:
      request_mode: responses
      reasoning_effort: high
      thinking_enabled: null
      responses:
        state: local
        cache:
          mode: implicit
```

### Inherit context policy but disable automatic summaries

<!-- example: context -->
```yaml
models:
  profiles:
    main:
      context:
        auto_snip: null
        auto_summarize: false
        auto_collapse: null
```

Null inherits the global switch here; it does not disable all compression. Disabling summaries may reduce extra summary requests but can reach capacity sooner.

### Disable a builtin skill by name

<!-- example: disabled-skill -->
```yaml
skills:
  disabled:
    - rcoder-config
```

Preserve other entries in an existing disabled list. The name disables the resolved skill, including user/workspace overrides. Direct YAML changes apply next start. The existing `/skills disable rcoder-config` session command updates the catalog immediately and persists workspace state. `/skills reload` rereads skill files, not just-edited configuration switches.

## Tool approval

Inspect effective approval.rules, default_mode and reviewer. Establish the requested tools/resources and scope. Matching uses specificity rather than simple list order; multiple resources use the strictest result.

Rules are a replacement list. Preserve rules that should remain active when adding one; a single allow rule must not accidentally remove all lower-layer rules. An empty list clears inheritance rather than restoring defaults. The tool name is shell. Resource patterns support exact values, directory/** and *, not arbitrary regex or command parsing.

Modes control tool exposure; approval authorizes invocation. Allow does not automatically expose a tool. Automatic review needs an explicit reviewer profile. Model connectivity does not validate approval response format, permissions or tool behavior.

## MCP, LSP and skills

- MCP uses mcp.servers.<name> with a stdio executable command, separate args, env and cwd. Processes run on the core host, often remote in VS Code Remote. Do not import mcpServers or HTTP/SSE fields from another client.
- LSP overrides use supported builtin language names. After config validity, use actual status/diagnostics to verify executable startup and file matching.
- Place a skill at workspace or user .rcoder/skills/<name>/SKILL.md with local relative resources. Leave installed builtin originals intact; use a same-name user/workspace override. Existing file permissions still apply.
- New/edited skill files can be discovered with /skills reload. No configuration hot reload is needed or provided.

## Troubleshooting

| Symptom | Check first |
| --- | --- |
| Saved value does not take effect | Saved file, actual host/workspace, explicit layer, app/profile inheritance, next-start versus session overrides |
| Configuration API command missing | Actual core Python and API version; PATH may select an older installation |
| Global edit has no project effect | Workspace/explicit override still wins; remove the intended override to inherit |
| Check passes but model rejects request | valid is structural; inspect startup/model results and protocol support |
| Thinking false still seems to reason | Nonempty effort wins; null uses provider defaults; UI text does not measure current computation |
| No visible reasoning | Returned deltas, preserve and UI state; encrypted Responses items are not readable text |
| MCP/LSP valid but unavailable | Executable, dependencies, cwd/env, process startup and handshake; config check does not run those services |
| Broken config prevents startup | Use the same core's standalone describe/inspect/check, restore only the intended edits/history and recheck; no running Agent is needed |
| Unknown fields or duplicate YAML keys | Follow describe; root must be an object, keys unique strings, no arbitrary Python objects or oversized documents |

Report only actual checks and meaningful outcomes, pending restart or external validation. Do not paste full credentials or sensitive raw logs.
