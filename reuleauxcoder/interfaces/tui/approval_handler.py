"""TUI approval handler — dispatches pending approvals to a queue.

The TUI main thread drains the queue and presents ``ApprovalDialog``
(a Textual ``ModalScreen``).  When the user clicks Allow/Deny, the
dialog calls ``pending.resolve()``, which sets ``threading.Event``
and wakes the agent thread.
"""

from __future__ import annotations

import queue

from reuleauxcoder.domain.approval import ApprovalHandler, PendingApproval


def make_tui_handler(approvals_queue: queue.Queue) -> ApprovalHandler:
    """Create a TUI approval handler that dispatches to a queue.

    The returned handler pushes ``PendingApproval`` onto
    *approvals_queue* and returns immediately.  The TUI dialog (on the
    main thread) is responsible for calling ``pending.resolve()``.
    """

    def handle(pending: PendingApproval) -> None:
        approvals_queue.put(pending)

    return handle
