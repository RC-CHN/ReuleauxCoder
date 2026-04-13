"""Builtin approval command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.app.runtime.approval import (
    VALID_APPROVAL_ACTIONS,
    build_approval_view,
    parse_approval_target,
    refresh_approval_runtime,
    same_rule_target,
)
from reuleauxcoder.infrastructure.persistence.workspace_config_store import WorkspaceConfigStore
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class SetApprovalRuleCommand:
    target: str
    action: str


def _parse_show_approval(user_input: str, parse_ctx):
    if matches_any(user_input, ("/approval", "/approval show")):
        return EmptyCommand()
    return None


def _parse_set_approval(user_input: str, parse_ctx):
    captures = match_template(user_input, "/approval set {target} {action}")
    if captures is None:
        return None

    try:
        target = non_empty_text().parse(captures["target"])
        action = non_empty_text().parse(captures["action"])
    except ParamParseError:
        return SetApprovalRuleCommand(target="", action="")

    return SetApprovalRuleCommand(target=target, action=action)


def _handle_show_approval(command, ctx) -> CommandResult:
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


def _handle_set_approval_rule(command, ctx) -> CommandResult:
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


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="approval.show",
                feature_id="approval",
                description="Show approval rules",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval show"),),
                parser=_parse_show_approval,
                handler=_handle_show_approval,
            ),
            ActionSpec(
                action_id="approval.set",
                feature_id="approval",
                description="Set approval rule",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval set <target> <action>"),),
                parser=_parse_set_approval,
                handler=_handle_set_approval_rule,
            ),
        ]
    )
