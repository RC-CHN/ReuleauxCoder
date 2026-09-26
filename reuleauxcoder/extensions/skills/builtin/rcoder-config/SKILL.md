---
name: rcoder-config
description: Configure rcoder through config.yaml, including models, reasoning/thinking, context, tool approval, MCP/LSP, skills and global/workspace settings.
---

# Configure rcoder itself

Configuration files are the persistent source of truth. Edit them with ordinary file tools and verify through read-only `describe`, `inspect` and `check`. Do not look for removed dedicated configuration-write tools. Explain results in the user's language.

## Locate, edit and verify

1. Read the core `python`, `workspace` and optional explicit `config` from `<skill_runtime>` beside the skill catalog. Invoke that Python with `-m reuleauxcoder config <operation> --workspace <workspace>`; always include `--config <config>` when present. Quote argv for the actual shell. A PATH installation may be a different version. If runtime metadata is absent, establish the real interpreter, host and launch file first.
2. Call `describe` for `api_version` and capabilities, then `inspect` for source paths, precedence and `model_targets`. This skill targets API v2; do not force its fields onto an older core. These commands can diagnose broken configuration without starting an Agent.
3. Follow the requested scope: project settings go to the workspace, defaults for all projects to the user file on the core host. Clarify only material ambiguity. An explicit launch file has highest precedence. VS Code Remote normally means the remote user's home; a relay tool host may differ from the core host.
4. Read the relevant local reference below. Understand effective values, then read the original target YAML and make the smallest change preserving unrelated fields, comments and concurrent edits. Deleting an override restores lower-layer inheritance. Lists replace as a whole; `null` only clears nullable fields.
5. Work within existing authorization. Use supplied or existing credentials without printing them. Never write `[configured]` redaction placeholders back to disk. `inspect.next_start` is an internal resolved object, not a YAML document to save. There is no generic environment-variable interpolation.
6. Run `check` against the same workspace and explicit file; the default checks are static + startup. Check unsaved YAML with `--content - --scope workspace|user|explicit`; multiple buffers use RPC `check.documents`. Use `--check model --profile <name>` only when model connectivity needs verification: it makes provider requests and consumes tokens. A failed probe is not necessarily invalid YAML. Fix invalid changes you introduced without discarding others' edits.
7. Report file/scope, meaningful changes, actual checks and activation timing. File edits apply on the next core start. Validate before an authorized restart; do not kill the serving agent through its own shell to test recovery. VS Code provides check/restart controls. `/skills reload` refreshes skill files, not config.yaml; session commands such as `/model` are not general configuration reloads.

## Load only the needed reference

- [references/configuration.md](references/configuration.md): all 19 sections and 133 field patterns, types, defaults, inheritance and constraints. Consult the target fields before editing; the running version's `describe` is authoritative.
- [references/reasoning.md](references/reasoning.md): effort, thinking, visible reasoning, replay, Responses encrypted items and protocol limits. Read when the user asks about reasoning or chain-of-thought return/display; identify which layer they mean.
- [references/workflows.md](references/workflows.md): commands, layered edits, troubleshooting and approval/MCP/skill examples. Examples are partial patches, not replacement documents.

## Keep the distinctions explicit

- Higher reasoning effort does not imply more visible reasoning. Configuration cannot recover hidden reasoning the provider does not return.
- Hiding reasoning in the UI does not disable thinking. Disabling visible-text preservation does not disable Responses encrypted replay.
- Tool approval controls permission to invoke tools, not diff styling or model capability. Change only the requested tools/resources.
- A larger local `max_context_tokens` does not expand the real model capacity; a smaller UI preview does not reduce model input.
- `check` is not end-to-end acceptance of MCP/LSP, tool use, images or tasks. Do not describe static validity as proof that everything works.
