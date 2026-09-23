# Runtime persistence lock ordering

The v0.10.5 audit started from a live `report_progress` hang on an ext4 HDD
workspace. Python stack sampling showed the tool waiting in snapshot `flush`
while a background snapshot waited in `PlanController.state`. This was a lock
cycle, not an ongoing file read. Slow storage can widen the overlap window;
the cycle itself does not depend on disk speed.

## Fixed cycles and ordering rules

| Paths | Previous cycle | Required order |
| --- | --- | --- |
| `report_progress`, `update_plan` vs background snapshot | controller → writer → controller | Commit ledger/state under controller lock; release it before persistence and event delivery. |
| Context replacement, first-message save, compression goal usage vs snapshot | context → writer → context | Both synchronous and deferred saves acquire context before the snapshot writer. |
| Steering drain vs compression cancellation checks | steering → context → steering | Acquire context before steering when applying admitted input. |
| RPC submission/admission/shutdown vs context-held goal publication | admission → context → admission | Keep admission and durable ledger mutation atomic; release admission before waiting for the snapshot. |

Plan/progress notifications retain the committed generation and return the
state from that commit. Existing presentation reducers ignore older revisions.
Goal mutations already release the goal lock before publishing.

The snapshot adapter keeps a consistent context view while serializing saves.
It still waits for real disk writes: this fix does not make fsync asynchronous
or promise that a busy/damaged disk cannot stall I/O. Initial session discovery,
ledger durability, snapshot-failure recovery and final shutdown saves remain.

`persist=False` on internal steering admission/discard skips only the immediate
snapshot, not the ledger write. RPC callers must flush after releasing admission
and before returning success. It is not a client-visible RPC option.

## Audit scope and verification

The review followed live snapshot capture, Plan/Progress and Goal publication,
message/context commits, steering, tool event journaling, runtime admission,
session reset/restore and shutdown. Output-journal/ledger writes and interaction
cancellation were checked for reverse callbacks while holding their state locks.
This is a focused runtime audit, not a proof about every extension or OS wait.

`tests/app/runtime/test_persistence_lock_order.py` forces the problematic
interleavings with thread events rather than timing sleeps. Each scenario runs
in a bounded subprocess so a deadlock regression cannot hang the test runner.
The cases cover both control tools, context replacement, first-message saves,
context-held Goal publication, steering drain, both steering RPC entries and
shutdown. Persistence tests also reload the resulting session.

The observed HDD process was blocked in Progress, not `read_file`. File reads
also pass through durable tool events and message persistence, but an earlier
read-specific hang cannot be assigned to the same cycle without its stack.
