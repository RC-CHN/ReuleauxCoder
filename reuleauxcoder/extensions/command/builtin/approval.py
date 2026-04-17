"""Builtin approval command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.module_registry import register_command_module
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
from reuleauxcoder.app.runtime.session_state import get_runtime_approval_config
from reuleauxcoder.infrastructure.persistence.workspace_config_store import WorkspaceConfigStore
from reuleauxcoder.interfaces.cli.views.common import render_markdown_panel
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.interfaces.view_registration import register_view


@dataclass(frozen=True, slots=True)
class SetApprovalRuleCommand:
    target: str
    action: str


@dataclass(frozen=True, slots=True)
class SetGlobalApprovalRuleCommand:
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


def _parse_set_global_approval(user_input: str, parse_ctx):
    captures = match_template(user_input, "/approval set-global {target} {action}")
    if captures is None:
        return None

    try:
        target = non_empty_text().parse(captures["target"])
        action = non_empty_text().parse(captures["action"])
    except ParamParseError:
        return SetGlobalApprovalRuleCommand(target="", action="")

    return SetGlobalApprovalRuleCommand(target=target, action=action)


@register_view(view_type="approval_rules", ui_targets={"cli"})
def render_approval_rules_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    markdown = payload.get("markdown")
    return isinstance(markdown, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown,
        title="Approval Rules",
    )


def _build_approval_payload(ctx) -> dict:
    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    view = build_approval_view(SimpleNamespace(approval=approval, mcp_servers=ctx.config.mcp_servers), ctx.agent)
    return view.to_payload()


def _handle_show_approval(command, ctx) -> CommandResult:
    payload = _build_approval_payload(ctx)
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


def _validate_approval_rule(command, ctx):
    if command.action not in VALID_APPROVAL_ACTIONS:
        ctx.ui_bus.error(
            "approval action must be one of allow, warn, require_approval, deny",
            kind=UIEventKind.APPROVAL,
        )
        return None

    rule = parse_approval_target(command.target, command.action)
    if rule is None:
        ctx.ui_bus.error(
            "target must be one of tool:<name>, mcp, mcp:<server>, or mcp:<server>:<tool>",
            kind=UIEventKind.APPROVAL,
        )
        return None
    return rule


def _handle_set_approval_rule(command, ctx) -> CommandResult:
    rule = _validate_approval_rule(command, ctx)
    if rule is None:
        return CommandResult(action="continue")

    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    approval.rules = [existing for existing in approval.rules if not same_rule_target(existing, rule)]
    approval.rules.append(rule)
    refresh_approval_runtime(ctx.agent, approval)

    payload = _build_approval_payload(ctx)
    ctx.ui_bus.success(
        "Updated session approval rule",
        kind=UIEventKind.APPROVAL,
        target=command.target,
        action_name=command.action,
    )
    ctx.ui_bus.refresh_view(
        "approval_rules",
        title="Approval Rules",
        payload=payload,
        reuse_key="approval_rules",
    )

    return CommandResult(action="continue", payload=payload)


def _handle_set_global_approval_rule(command, ctx) -> CommandResult:
    rule = _validate_approval_rule(command, ctx)
    if rule is None:
        return CommandResult(action="continue")

    ctx.config.approval.rules = [
        existing for existing in ctx.config.approval.rules if not same_rule_target(existing, rule)
    ]
    ctx.config.approval.rules.append(rule)
    path = WorkspaceConfigStore().save_approval_config(ctx.config.approval)
    setattr(ctx.agent, "session_approval_config", None)
    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    refresh_approval_runtime(ctx.agent, approval)

    payload = _build_approval_payload(ctx)
    ctx.ui_bus.success(
        f"Updated global approval rule and saved to {path}",
        kind=UIEventKind.APPROVAL,
        target=command.target,
        action_name=command.action,
        saved_path=str(path),
    )
    ctx.ui_bus.refresh_view(
        "approval_rules",
        title="Approval Rules",
        payload=payload,
        reuse_key="approval_rules",
    )

    return CommandResult(action="continue", payload={"saved_path": str(path), **payload})


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="approval.show",
                feature_id="approval",
                description="Show effective approval rules for the current session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval show"),),
                parser=_parse_show_approval,
                handler=_handle_show_approval,
            ),
            ActionSpec(
                action_id="approval.set",
                feature_id="approval",
                description="[session] Set a session approval rule override",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval set <target> <action>"),),
                parser=_parse_set_approval,
                handler=_handle_set_approval_rule,
            ),
            ActionSpec(
                action_id="approval.set_global",
                feature_id="approval",
                description="[global] Set a global approval rule default",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval set-global <target> <action>"),),
                parser=_parse_set_global_approval,
                handler=_handle_set_global_approval_rule,
            ),
        ]
    )
