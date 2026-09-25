# Configuration inspection API

Configuration files are the editable source of truth. The core owns merging,
defaults and validation; editors and ordinary file tools own edits. The
[configuration reference](configuration-reference.md) lists every YAML field.

## Contract

API version **2** exposes exactly three read-only operations. Normal runtime
initialization advertises `configuration_api: 2`. Python and TypeScript clients
use `RuntimeClient.configuration`; the shared TypeScript package also exports
`ConfigurationClient`. Responses are plain JSON.

| RPC method | Parameters | Result |
| --- | --- | --- |
| `config.describe` | Optional `section` | API/core versions, JSON schema, scopes, checks, capabilities |
| `config.inspect` | None | Disk revision, redacted source layers, effective next-start values, running configuration summary when attached, model targets, diagnostics |
| `config.check` | Optional `checks`, `profiles`, `documents`, `base_revision` | Static validity, separate check results, model targets, diagnostics, revision and whether buffers were included |

Capabilities are `buffer_checks` and `profile_probes`. There are no dedicated
model configuration tools, write RPC methods, prepared candidates, apply/revert
operations or validation leases. Existing private backups from earlier development
builds are left untouched; this API does not read or manage them.

The independent `rcoder config rpc` process works without a valid configuration,
Agent, restored session or frontend. VS Code launches the configured workspace-host
command with `--config-management-stdio`, preserving its explicit `--config`
and `--cwd`. Its initialization includes `mode: configuration` and
`api_version: 2`. An old core requires an update, not a write-protocol fallback.
Relay clients can describe/inspect; probing is local-only.

## Sources and editing

Precedence, low to high: built-in defaults → `user`
(`~/.rcoder/config.yaml`) → `workspace` (`.rcoder/config.yaml`) →
`explicit` (the optional `--config` file). All paths belong to the core host;
in VS Code Remote, global means the remote user's defaults, not desktop settings.

Ordinary dictionaries merge recursively and lists replace the previous list.
Removing a workspace override reveals the inherited value. `null` only clears
fields that support it; it is not a universal reset. Inspection returns both
raw source values and the resolved next-start Config; they have different layouts.
Do not serialize the resolved Config back as a YAML source.

Reading, inspection, checks and startup never generate example files or backfill
workspace settings. File edits take effect at the next core start; runtime
session actions keep their existing behavior. There is no automatic config reload.

Unsaved documents are checked in memory using the same core resolver:

```json
{
  "checks": ["static", "startup"],
  "base_revision": "<revision from inspect>",
  "documents": [
    {"scope": "workspace", "content": "ui:\n  verbosity: standard\n"}
  ]
}
```

Supply one to three complete UTF-8 YAML buffers, each at most 1 MiB. Unspecified
layers come from disk. Buffers referring to the same physical file must agree;
the replacement applies to all aliases. Content is not returned in diagnostics.
Every source, including shadowed layers, contributes to the disk revision. An
optional stale `base_revision` or disk changes during checks produce a conflict.
This is diagnostic consistency, not a lock on arbitrary external editors.

VS Code uses native TextDocuments for edits, saves and undo, preserving comments,
ordering and line endings. It checks dirty configuration buffers, invalidates
results on edits/saves, and rejects results received after a buffer changed.
Saving only touches dirty configuration documents and uses VS Code's external
file conflict handling. Use editor Undo or available file Timeline history for
manual recovery; Timeline is not guaranteed to record external edits.

The UI starts with **Current workspace only**, folds **Global defaults · all
projects**, and identifies an explicit launch file as highest priority. Checking
always combines the layers. Model connection tests are optional. Save and restart
are separate actions; restarting checks saved files before stopping a running
core, and refuses dirty files or an active turn. Invalid configuration leaves the
existing core running.

## Checks

- `static`: strict UTF-8/YAML, duplicate keys, unknown fields, types/ranges,
  references, provider/request-mode compatibility and merged configuration.
  This is the same validation used by ordinary startup. `valid` means static
  validity only.
- `startup`: bounded isolated construction of provider clients for all resolved
  profiles. No Agent, goal restoration, hooks, MCP/LSP launch or model request.
- `model`: optional bounded text request through the selected profile's actual
  provider and configured request parameters. Select one to eight profile names;
  without `profiles`, check the main profile. Each probe is bounded to 20 seconds
  and may consume provider tokens. No workspace data, tools or images are sent.
  Authentication/request failures are failed checks; transient failures/timeouts
  may be `unknown`. A failed connection does not make valid YAML invalid.

Checks default to static plus startup. A static result is always included.
There are no cached verification permissions or online checks required for saving.
Credentials, MCP environment/argument values and credential-bearing URLs are
redacted in inspection. File/shell access and ordinary tool approvals govern
Agent editing; the read-only API is not an authorization boundary for files.

## CLI examples

```bash
rcoder config describe --section models
rcoder config inspect
rcoder config check
rcoder config check --content draft.yaml --scope workspace
rcoder config check --check model --profile main --profile sub
rcoder config inspect --config /absolute/path/to/config.yaml
rcoder config rpc --workspace /path/to/project
```

`--content -` reads raw YAML from stdin. `--revision` passes an expected disk
revision. Exit codes: 0 for successful requested checks, 1 for invalid config or
failed/unknown checks, 2 for invalid inputs/conflicts. To change configuration,
read the intended source, edit the file, check it, then explicitly restart when
appropriate. Never echo credentials into a task report.
