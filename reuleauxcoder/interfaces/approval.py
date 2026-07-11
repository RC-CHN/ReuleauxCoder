"""Shared approval-to-interaction adapter used by every interface."""

from __future__ import annotations

from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalHandler,
    PendingApproval,
)
from reuleauxcoder.interfaces.interactions import (
    ReviewContext,
    ReviewRequest,
    UIInteractor,
)


def make_approval_handler(ui_interactor: UIInteractor) -> ApprovalHandler:
    """Resolve domain approvals through one typed review interaction."""

    def handle(pending: PendingApproval) -> None:
        request = pending.request
        subagent_summary = ""
        if request.metadata.get("is_subagent"):
            subagent_mode = request.metadata.get("subagent_mode") or "unknown"
            subagent_task = str(request.metadata.get("subagent_task") or "").strip()
            if len(subagent_task) > 200:
                subagent_task = subagent_task[:180] + "..."
            subagent_summary = f"\nSource: sub-agent (mode={subagent_mode})"
            if subagent_task:
                subagent_summary += f"\nSub-agent task: {subagent_task}"

        response = ui_interactor.review(
            ReviewRequest(
                title=f"Approval required: {request.tool_name}",
                summary=(
                    f"Tool '{request.tool_name}' from source '{request.tool_source}'"
                    f" requires approval.{subagent_summary}"
                ),
                sections=(
                    request.preview.sections if request.preview is not None else ()
                ),
                context=ReviewContext(
                    tool_name=request.tool_name,
                    tool_source=request.tool_source,
                    reason=request.reason,
                    is_subagent=bool(request.metadata.get("is_subagent")),
                    subagent_mode=request.metadata.get("subagent_mode"),
                    subagent_task=request.metadata.get("subagent_task"),
                ),
            )
        )

        if response.approved:
            pending.resolve(
                ApprovalDecision.allow_once(
                    response.reason or "approved via interaction"
                )
            )
        else:
            pending.resolve(
                ApprovalDecision.deny_once(response.reason or "denied via interaction")
            )

    return handle
