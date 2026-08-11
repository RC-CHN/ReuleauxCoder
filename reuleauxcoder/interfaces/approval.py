"""Shared approval-to-interaction adapter used by every interface."""

from __future__ import annotations

from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalHandler,
    PendingApproval,
)
from reuleauxcoder.interfaces.interactions import (
    ReviewContext,
    ReviewGrantOption,
    ReviewRequest,
    UIInteractor,
)

_READ_ONLY_TOOLS = frozenset(
    {"read_file", "list_file", "glob", "grep", "lsp", "lsp_status"}
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

        if request.tool_name in _READ_ONLY_TOOLS:
            approval_summary = "Read-only workspace access."
        else:
            approval_summary = (
                f"Tool '{request.tool_name}' from source "
                f"'{request.tool_source}' requires approval."
            )
        approval_summary += subagent_summary
        operation = request.metadata.get("approval_operation")
        if isinstance(operation, str) and operation:
            approval_summary = f"Operation: {operation}\n" + approval_summary
        if request.subjects:
            label = "Target" if len(request.subjects) == 1 else "Targets"
            approval_summary += f"\n{label}: {', '.join(request.subjects)}"
        if request.metadata.get("workspace_changed_during_approval"):
            approval_summary = (
                "Workspace changed while approval was pending. "
                "Review the refreshed diff.\n" + approval_summary
            )

        response = ui_interactor.review(
            ReviewRequest(
                title=f"Approval required: {request.tool_name}",
                summary=approval_summary,
                sections=(
                    request.preview.sections if request.preview is not None else ()
                ),
                context=ReviewContext(
                    tool_name=request.tool_name,
                    tool_source=request.tool_source,
                    operation=(
                        operation if isinstance(operation, str) else None
                    ),
                    subjects=request.subjects,
                    reason=request.reason,
                    is_subagent=bool(request.metadata.get("is_subagent")),
                    subagent_mode=request.metadata.get("subagent_mode"),
                    subagent_task=request.metadata.get("subagent_task"),
                ),
                grant_options=tuple(
                    ReviewGrantOption(
                        id=candidate.id,
                        label=candidate.label,
                        description=candidate.description,
                        broad=candidate.broad,
                    )
                    for candidate in request.grant_candidates
                ),
                queue_status=request.queue_status,
                request_id=request.request_id,
            )
        )

        if response.action == "allow_session":
            selected = next(
                (
                    candidate
                    for candidate in request.grant_candidates
                    if candidate.id == response.selected_id
                ),
                None,
            )
            if selected is None:
                pending.resolve(
                    ApprovalDecision.deny_once(
                        "invalid session approval scope returned by interaction"
                    )
                )
                return
            pending.resolve(
                ApprovalDecision.allow_session(
                    selected,
                    response.reason or f"approved for session: {selected.label}",
                    reviewed=True,
                )
            )
        elif response.action == "allow_once":
            pending.resolve(
                ApprovalDecision.allow_once(
                    response.reason or "approved via interaction",
                    reviewed=True,
                )
            )
        else:
            pending.resolve(
                ApprovalDecision.deny_once(
                    response.reason or "denied via interaction",
                    reviewed=not response.cancelled,
                )
            )

    return handle
