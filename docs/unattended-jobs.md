# Unattended jobs

`rcoder job` runs the shared runtime with a durable job identity, an independent
session and machine-readable control files. It accepts ordinary user instructions
and a working directory. The caller supplies any setup instructions as task text;
the job frontend contains no platform-specific API client or bundled onboarding
skill. Normal workspace tools, configuration, skill discovery and approval policy
remain in effect.

## Start and supervise

Save the complete task paragraph in a UTF-8 file, then run:

```sh
rcoder job start --id build-report-001 --workspace /work/report \
  --task-file /private/instructions.txt --detach \
  --token-budget 100000 --max-seconds 14400
rcoder job status build-report-001
rcoder job logs build-report-001 --after 0 --limit 100
rcoder job result build-report-001
```

`--task "..."` accepts inline text; `--task-file -` reads UTF-8 stdin. Instructions
are submitted as chat text, including strings starting with `/`. For instructions
longer than 4000 characters, provide a short `--objective`; the full instructions
are still sent unchanged. `--config` and `--model` have the normal runtime meaning.
The workspace must already exist. Use separate worktrees/directories for parallel
tasks. A job never automatically restores another job's latest session.

Omit `--detach` to remain in the foreground without terminal interaction. Both
frontends print JSON to stdout; runtime diagnostics use stderr or the detached
`worker.log`. `rcoder-cli job` supports the same commands and needs no Node/TUI.

Detached launch waits up to ten seconds for worker admission. Its `launch_state`
is `admitted`, `failed`, or `pending`; `pending` means the caller must inspect
status/logs, not that execution has started. Runtime failures after admission are
reported in the durable status and result. An occupied workspace is a launch
failure. A repeated start with the same ID and identical specification returns
the existing job, without another execution; a different specification conflicts.
If a launcher dies after creating a queued job, explicitly resume that ID.

## Stop and resume

```sh
rcoder job pause build-report-001
rcoder job resume build-report-001 --detach
rcoder job cancel build-report-001
rcoder job list
```

Pause/cancel target the current active attempt. They interrupt work and pending
interactions and wait for runtime cleanup. SIGINT/SIGTERM request a pause.
`resume` restores exactly the recorded session, goal and accumulated usage.
A missing/corrupt recorded session fails rather than creating a replacement.
Completed jobs return their result without restarting. Cancellation is terminal;
use a new ID to authorize a new run. Failed verification can be repeated by resume.

Token budgets count the existing goal engine's uncached input plus output and
persist across resume. `--max-seconds` bounds cumulative active job time, including
startup, interaction waits and checks, excluding time between attempts. Resume can
explicitly raise either limit; it never resets used tokens or elapsed time. Without
limits, the normal goal configuration applies. Completion comes from the goal
engine and any declared checks below, not from the end of a single model turn.

The job command is a worker, not a resident scheduling service. An external
supervisor can poll status and choose when to resume an interrupted or blocked
job. Exhausted provider retries and questions remain visible; the worker does not
turn failure into completion. Time limits request cooperative interruption, so
an unresponsive external tool or OS operation may delay shutdown.

## Questions and approvals

Existing policy decides which operations may proceed automatically. `job` does not
change policy to allow-all. Pending questions/approvals appear in
`status.pending_requests`, with request ID, attempt ID and structured request.
The state becomes `waiting_input`; there is no stdin prompt. Answer from a private
UTF-8 JSON file:

```sh
rcoder job answer build-report-001 REQUEST_ID --answer-file /private/answer.json
```

| Kind | Response |
| --- | --- |
| `input_text` | `{"value":"The requested answer"}` |
| `choose_one` | `{"selected_id":"one advertised choice ID"}` |
| `confirm` | `{"confirmed":true}` |
| `review` | `{"approved":true,"reason":"Reviewed the concrete operation"}` |

Approval grants apply once, through the shared runtime's existing authorization
and snapshot checks. File reviews include frozen before/after text, paged through
the existing RPC contract. Oversize review documents are rejected rather than
presented as a complete preview. Answers are bound to one attempt/request; stale,
conflicting or post-stop answers are rejected. A caller may also use the normal
configured automatic reviewer. A job needing clarification remains pending until
answered, interrupted or limited.

## Declared checks and artifacts

Use a versioned JSON specification for reproducible acceptance. Do not mix `--spec`
with task specification flags; `--id` and `--detach` still apply.

```json
{
  "version": 1,
  "workspace": "/work/report",
  "prompt": "Implement the report generator and save the finished report.",
  "objective": "Deliver a verified report generator",
  "token_budget": 100000,
  "max_seconds": 14400,
  "artifacts": ["output/report.pdf"],
  "checks": [{"argv": ["python", "-m", "unittest"], "timeout_seconds": 300}]
}
```

```sh
rcoder job start --id report-002 --spec /private/job.json --detach
```

After goal completion, checks execute in the workspace with explicit argv and
closed stdin; their exit codes, timeouts and log paths are recorded. Required
artifact paths must resolve to files inside the workspace. Their final size and
SHA-256 are collected after checks. A missing artifact or failing check gives
`verification_failed`. Without declared checks/artifacts, the result explicitly
uses `verification.basis: "agent_reported"`.

## Persistence, races and handover

Default registry: `~/.rcoder/jobs/<job_id>`; override with
`rcoder job --store /private/registry ...`. Each job stores:

- Immutable `spec.json`, atomic `state.json`, `events.jsonl` with monotonic cursors.
- Job-private runtime `sessions/`, including the shared runtime's durable ledger.
- `requests/` and `answers/`, scoped to an attempt.
- `attempts/<attempt_id>.json`, check logs, and the latest `result.json`.
- Stable `RCODER_JOB_ID` and per-attempt `RCODER_JOB_ATTEMPT_ID` in the worker environment.

The last model response, goal status, token/time usage, checks/artifacts and errors
are the result handover. Publish or copy it using the caller's chosen system.
Task text, model output and pending requests can contain private data: protect the
registry with the current user's filesystem permissions. POSIX directories/files
are created as 0700/0600; Windows uses the directory's ACL inheritance.

OS leases serialize each job and each workspace across registries. Locks are
released by the OS on process death; lock files are never deleted to steal a lock.
Concurrent starts/resumes cannot execute the same workspace twice. Atomic snapshots
and a ledger check prevent replaying an already admitted initial prompt. Accepted
stop requests and terminal publication share a lock, so a stop accepted before
completion wins. The terminal state is published after the result file.

A killed process may already have changed files or external systems. Recovery
retains the original runtime ledger; task-specific side effects still need their
own idempotency/reconciliation. The workspace lease coordinates job frontends;
interactive sessions and unrelated applications do not participate in it.

| Worker exit | Result |
| --- | --- |
| 0 | completed |
| 1 | failed |
| 2 | command/launch error |
| 3 | blocked |
| 4 | paused |
| 5 | budget_limited |
| 6 | usage_limited |
| 7 | time_limited |
| 8 | verification_failed |
| 130 | cancelled |

Read-only status/result/log commands return zero when the query succeeds; inspect
the stored `status`/`exit_code` for the worker outcome.

## Tests

`test_jobs.py` exercises the real runtime/session ledger with an in-process fake
model loop. `test_job_cli.py` launches actual detached workers against a local test
HTTP model endpoint. Tests include duplicate creation, workspace contention, kill
and concurrent resume, initial prompt admission, cancellation versus completion,
stale answers, frozen reviews, cumulative limits, missing sessions, check timeout,
artifact hashes and interrupted event-log tails. These tests require no external
collaboration service or paid model credentials.
