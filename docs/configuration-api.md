# Configuration management API

The core exposes one `ConfigurationService` to model tools, JSON-RPC clients and
`rcoder config`. Management does not require an Agent, a valid model credential,
session restoration or a terminal frontend to start. An invalid configuration
can therefore be inspected and repaired through the standalone management process.

## Contract

Normal runtime initialization advertises `configuration_api: 1`. Python clients
use `RuntimeClient.configuration`; TypeScript clients use the same property or
the exported `ConfigurationClient` from `@reuleauxcoder/client`. Management
responses are plain JSON, independent of the runtime's dataclass codec.

| RPC method | Parameters | Result |
| --- | --- | --- |
| `config.describe` | Optional `section`, e.g. `models` | API/core versions, editable JSON schema, scopes, activation and probe capabilities |
| `config.inspect` | None | Revision, redacted source files, next-start configuration, running configuration summary when attached, diagnostics |
| `config.prepare` | `scope`, optional `base_revision`, exactly one of `changes` / `document` | Durable candidate ID, redacted diff, target path, diagnostics, required probes |
| `config.validate` | Optional `change_id`, `checks` | Static validity, separate probe results and diagnostics |
| `config.apply` | `change_id`, optional human-only `allow_unverified_model` | Applied record with `activation: next_start` |
| `config.history` | Optional `limit`, 1–100 | Recent candidate/commit records without credentials or backup contents |
| `config.revert` | `change_id` | A new candidate restoring the previous target file; later edits cause a conflict |
| `config.recover` | `change_id`, current `base_revision`, optional `side: before/after` | A human-requested recovery candidate, including repair of externally corrupted YAML |

Persistent scopes are `workspace`, `user` and, when the process was launched with
an explicit configuration path, `explicit`. Paths belong to the workspace host;
in VS Code Remote this is the remote host. `user` does not mean the desktop's user
configuration. The service binds its paths once and never changes them after a
shell tool changes directory. Remote relay clients cannot mutate host configuration
through the management RPC methods.

This API version saves persistent settings for the **next core start**. It does
not replace the running Agent, terminate the current request, or claim that a
saved change has taken effect. Existing session model/mode commands keep their
runtime behavior. Session-only configuration mutation and a configuration UI are
not part of this API version. `inspect.runtime` is null in a standalone process.

Changes use RFC 6901 JSON pointers, preserving profile names containing dots:

```json
{
  "scope": "workspace",
  "base_revision": "<revision returned by inspect>",
  "changes": [
    {"path": "/ui/verbosity", "value": "standard"},
    {"path": "/models/profiles/my.model/temperature", "value": 0.2},
    {"path": "/ui/max_preview_lines", "remove": true}
  ]
}
```

Removal removes the value from that source, allowing lower-priority defaults to
be inherited. Lists are replaced as complete fields. A complete `document` is
available to human clients to repair an unparseable file. Prepare never modifies
the target file. Candidates expire after one hour; at most 128 unexpired prepared
candidates are accepted in one workspace context.

## Validation and activation

Validation distinguishes:

- `static`: strict UTF-8/YAML shape, duplicate keys, field types/ranges, references,
  provider/request-mode compatibility and the merged persistent configuration.
  Reading or checking never creates example files, backfills settings or starts
  external services. `valid` describes these static checks only.
- `startup`: a bounded child process constructs configuration and the provider
  client. It does not start an Agent, restore goals, run hooks, launch MCP/LSP
  servers or make a model request. This checks the configuration/provider startup
  path, not every possible runtime dependency.
- `model`: one bounded minimal request through the selected Chat Completions,
  Responses or Anthropic Messages adapter, without workspace data, tools,
  orchestration retries or diagnostic dumps. It can consume provider tokens.
  Authentication/request errors fail; timeouts, throttling and unavailable
  services are reported as `unknown`, separately from invalid configuration.

Apply always rechecks static validity and isolated startup. When effective active
model settings change, it also requires a successful model probe within five
minutes. Human clients can explicitly use `allow_unverified_model` to save an
offline recovery/setup configuration; the result records `model_verified: false`.
Models cannot request this override. Probe success is evidence about that check,
not a guarantee of future network availability or success on arbitrary tasks.

Every source file contributes to the revision, including shadowed layers. Probes
and human approval do not hold configuration locks. Commit acquires a short
cross-process lock, rechecks the revision and any connected editor's unsaved-file
guard, journals the change, then atomically replaces the target file. The current
runtime remains usable throughout. Arbitrary external editors do not participate
in the lock; edits observed before commit invalidate the candidate.

An interrupted commit remains visible as `committing`. Retrying apply identifies
whether the replacement occurred and finishes the journal without repeating a
completed write; divergent files require an explicit repair. Revert and recover
prepare new candidates and go through the same validation/application gates.
Recovery never automatically resumes tasks, installs services or relaxes a policy.

Private candidates and before/after snapshots live under
`~/.rcoder/config-management/<workspace-context>/`, outside the project. Files are
created with private permissions on POSIX; Windows uses the owning directory's
ACL. Backups can contain credentials and are never returned by the API. API keys,
MCP environment/argument values and credential-bearing URLs are redacted from
inspection, diffs and history. Generated errors do not echo provider responses or
invalid YAML contents.

## Model tools and authorization

`config_read` exposes describe/inspect/history. `config_prepare` prepares edits or
revert candidates. `config_validate` runs selected checks. `config_apply` applies
one immutable candidate ID. The latter two use ordinary tool authorization;
defaults request approval. Apply's review includes the target, scope, actual field
changes and validation results. A change made during review is rejected at commit.

The core fixes caller identity; parameters cannot promote a model to a human.
Models cannot replace whole documents, submit credential/process-argument fields,
change approval/mode/relay policy, use human-prepared candidates, or bypass live
model validation. Root configuration services are not copied into subagent tools.
These checks govern management interfaces; ordinary filesystem/shell access
continues to follow its own tool policies.

## CLI and recovery transport

All CLI commands return UTF-8 JSON. Exit code 0 is success, 1 means failed/unknown
validation, and 2 means an invalid request, conflict or operation failure.

```bash
rcoder config describe --section models
rcoder config inspect
rcoder config check                    # offline static + isolated startup
rcoder config prepare --changes changes.json --revision <revision>
rcoder config validate <change-id> --check startup --check model
rcoder config apply <change-id>
rcoder config history
rcoder config revert <change-id>       # returns a new candidate; apply separately
rcoder config recover <change-id> --revision <revision> --side before
rcoder config rpc                      # standalone JSON-RPC over stdio
```

Use `--workspace` and `--config` to select the same host/workspace/explicit layer
when recovering. JSON inputs can use `--document -` or `--changes -` to read stdin;
credentials need not appear in process arguments. A standalone RPC client calls
`initialize` with `{"version":1}` and receives `mode: configuration`. Close stdin
to stop it. Do not call runtime readiness or task methods on this transport.

Regression coverage includes all three provider transports with local fake
servers, cross-process locking, interrupted writes, expired candidates/probes,
concurrent edits during approval, dirty editor buffers, secret redaction, Unicode
paths, model authority, and actual Python/TypeScript recovery processes.
