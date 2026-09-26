# Persistent goals

Open `/goal` in the TUI to create, inspect, edit, pause, resume or clear the
current session's goal. The sidebar shows its actual status, objective and usage;
the panel and F2 session details retain the full objective on narrow terminals.
You can also explicitly ask the model to create a goal for a task.

CLI commands use the same backend actions:

```text
/goal
/goal create Finish the migration and verify every public entrypoint
/goal edit Finish the migration, including the remote entrypoint
/goal pause
/goal budget 500000
/goal budget none
/goal resume
/goal clear
```

`/goal create`, `/goal edit` and `/goal budget` without arguments open text
interactions. Editing preserves the goal's status and cumulative usage. There is
one goal per session; finish or clear an unfinished goal before creating another.

## Configuration and accounting

Workspace `.rcoder/config.yaml` and user `~/.rcoder/config.yaml` accept:

```yaml
goal:
  default_token_budget: null
```

The default is **no limit**. Set a positive integer for newly created goals.
An explicitly supplied model-tool budget overrides this default. The resolved
budget is stored with the goal: changing configuration, restarting, resuming or
compacting the session does not reset its usage or change its budget.

Goal tokens use the Codex convention:

```text
input tokens - cached input tokens + output tokens
```

Provider adapters normalize total input before accounting. Anthropic's input,
cache-creation and cache-read buckets are added first, then cache reads are
deducted, following its [usage definition](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

Main-model requests, summary/repair requests and goal-owned isolated subagent
requests are accounted through the common LLM request boundary. Missing cache
statistics mean no cache deduction. Responses without input usage use a labelled
character-based estimate; interrupted streams retain reported or estimated usage
when chunks have arrived. Requests that fail before any chunk have no measurable
usage here. This is a work budget, not an exact provider bill.

An in-flight request stays attributed to the goal that owned it at dispatch.
Subagents keep their originating goal ID across checkpoints. Late usage from a
cleared/replaced goal cannot charge its replacement. Pausing blocks new main
requests from being charged to that goal; already-running requests and its child
jobs can still report usage.

Reaching the budget marks the goal `budget_limited`, requests a short wrap-up and
prevents further automatic turns. The current turn's wrap-up remains accounted,
so the final count may exceed the limit. Increasing or removing the limit does
not automatically resume a stopped goal: choose Resume explicitly.

## Execution and persistence

The backend starts another regular turn only when the goal is `active`, no
user/client operation remains, and the mode allows goal completion tools.
Planner mode does not automatically continue. A model final answer ends a turn;
`update_goal(status="complete")` ends the goal.

Continuation is tagged internal context. It does not create a human message in
the transcript. Every request receives the current goal state, including after
compaction. The model can use the existing history tools for original details.

Pause lets the current turn finish but disables continuation. The interrupt
gesture also pauses the goal and retains the existing stop/steering behavior.
The explicit immediate-guidance action uses `runtime.interrupt` with
`steering_only: true` and the current `session_generation`. It promotes admitted
messages without pausing the goal or stopping the turn. Repeated requests and
requests racing with message application are harmless; ordinary interrupt and
stop gestures retain their existing semantics. Frontends check the
`steering_promotion` initialization capability before offering this action.
Creating or resuming through `/goal` waits for the current turn to exit and clears
its old stop signal before starting work.
Clear removes the goal without cancelling the current turn or managed processes.
Unrecoverable turn failures block the goal; HTTP 429 failures are shown as
`usage_limited`. User/client commands are admitted before automatic continuation.

Goal changes are appended to the existing session ledger. The runtime snapshot
stores its current projection; recovery uses a newer ledger record if present,
including a clear event. A resumed `active` goal can continue once the frontend
has received its initial state and acknowledged readiness. Paused, blocked,
limited and complete goals remain stopped. Time records active runtime elapsed
time and excludes time while the process is closed.

Model tools are root-only: `create_goal`, `get_goal`, `update_goal`. The model
cannot pause, resume, edit the objective or change the existing budget.
Completion requires current evidence for all requirements. The three-turn
blocked audit is a model instruction, not a separate automated verifier. Editing
or replacing a goal while a model request is in flight invalidates completion
against that request's old objective; the model must read and reassess the goal.

Goal admission, persistence and accounting are owned by the backend. CLI/TUI
actions cross `runtime.submit` over JSON-RPC, snapshots include the goal, and
`goal.get` is a read-only query. `runtime.ready` acknowledges initial state before
restored goals are allowed to start. Open panels receive ordinary view refreshes;
the CLI only prints goal details on request, avoiding per-request status spam.
