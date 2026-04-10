"""Shared handlers for approval-related commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import (
    CommandContext,
    CommandResult,
    OpenViewRequest,
    SetApprovalRuleCommand,
    ShowApprovalCommand,
)
from reuleauxcoder.app.runtime.approval import (
    VALID_APPROVAL_ACTIONS,
    build_approval_view,
    parse_approval_target,
    refresh_approval_runtime,
    same_rule_target,
)
from reuleauxcoder.infrastructure.persistence.workspace_config_store import WorkspaceConfigStore
from reuleauxcoder.interfaces.events import UIEventKind


def handle_show_approval(command: ShowApprovalCommand, ctx: CommandContext) -> CommandResult:
    """Build and publish the structured approval view."""
    view = build_approval_view(ctx.config, ctx.agent)
    payload = view.to_payload()
    ctx.ui_bus.open_view(
        "approval_rules",
        title="Approval Rules",
        payload=payload,
        reuse_key="approval_rules",
    )
    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="approval_rules",
                title="Approval Rules",
                payload=payload,
                reuse_key="approval_rules",
            )
        ],
        payload=payload,
    )


def handle_set_approval_rule(command: SetApprovalRuleCommand, ctx: CommandContext) -> CommandResult:
    """Update one approval rule and refresh runtime config."""
    if command.action not in VALID_APPROVAL_ACTIONS:
        ctx.ui_bus.error(
            "approval action must be one of allow, warn, require_approval, deny",
            kind=UIEventKind.APPROVAL,
        )
        return CommandResult(action="continue")

    rule = parse_approval_target(command.target, command.action)
    if rule is None:
        ctx.ui_bus.error(
            "target must be one of tool:<name>, mcp, mcp:<server>, or mcp:<server>:<tool>",
            kind=UIEventKind.APPROVAL,
        )
        return CommandResult(action="continue")

    ctx.config.approval.rules = [
        existing for existing in ctx.config.approval.rules if not same_rule_target(existing, rule)
    ]
    ctx.config.approval.rules.append(rule)
    path = WorkspaceConfigStore().save_approval_config(ctx.config.approval)
    refresh_approval_runtime(ctx.agent, ctx.config.approval)

    ctx.ui_bus.success(
        f"Updated approval rule and saved to {path}",
        kind=UIEventKind.APPROVAL,
        target=command.target,
        action_name=command.action,
        saved_path=str(path),
    )

    view = build_approval_view(ctx.config, ctx.agent)
    ctx.ui_bus.refresh_view(
        "approval_rules",
        title="Approval Rules",
        payload=view.to_payload(),
        reuse_key="approval_rules",
    )

    return CommandResult(action="continue", payload={"saved_path": str(path)})
