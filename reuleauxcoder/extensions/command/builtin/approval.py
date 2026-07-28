"""Builtin approval command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
    PanelRefreshPolicy,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.runtime.approval import (
    ApprovalRuleView,
    ApprovalView,
    VALID_APPROVAL_ACTIONS,
    build_approval_view,
    parse_approval_target,
    refresh_approval_runtime,
    same_rule_target,
)
from reuleauxcoder.app.runtime.session_state import get_runtime_approval_config
from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class SetApprovalRuleCommand:
    target: str
    action: str


@dataclass(frozen=True, slots=True)
class SetGlobalApprovalRuleCommand:
    target: str
    action: str


@dataclass(frozen=True, slots=True)
class UnsetApprovalRuleCommand:
    target: str


@dataclass(frozen=True, slots=True)
class UnsetGlobalApprovalRuleCommand:
    target: str


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


def _parse_unset_approval(user_input: str, parse_ctx):
    captures = match_template(user_input, "/approval unset {target}")
    if captures is None:
        return None

    try:
        target = non_empty_text().parse(captures["target"])
    except ParamParseError:
        return UnsetApprovalRuleCommand(target="")

    return UnsetApprovalRuleCommand(target=target)


def _parse_unset_global_approval(user_input: str, parse_ctx):
    captures = match_template(user_input, "/approval unset-global {target}")
    if captures is None:
        return None

    try:
        target = non_empty_text().parse(captures["target"])
    except ParamParseError:
        return UnsetGlobalApprovalRuleCommand(target="")

    return UnsetGlobalApprovalRuleCommand(target=target)


def _build_approval_view(ctx):
    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    view = build_approval_view(
        SimpleNamespace(approval=approval, mcp_servers=ctx.config.mcp_servers),
        ctx.agent,
    )
    return view


def _parse_unset_target(command, ctx):
    if not command.target:
        ctx.effect.error(
            "target must be one of tool:<name>, mcp, mcp:<server>, or mcp:<server>:<tool>",
            kind=UIEventKind.APPROVAL,
        )
        return None
    # Reuse the target grammar without requiring an action.
    rule = parse_approval_target(command.target, "allow")
    if rule is None:
        ctx.effect.error(
            "target must be one of tool:<name>, mcp, mcp:<server>, or mcp:<server>:<tool>",
            kind=UIEventKind.APPROVAL,
        )
        return None
    return rule


def _handle_unset_approval_rule(command, ctx) -> CommandEffect:
    rule = _parse_unset_target(command, ctx)
    if rule is None:
        return ctx.effect.finish(control="continue")

    session_rules = list(getattr(ctx.agent, "session_approval_rules", []) or [])
    remaining = [
        existing for existing in session_rules if not same_rule_target(existing, rule)
    ]
    if len(remaining) == len(session_rules):
        ctx.effect.error(
            f"No session approval rule for '{command.target}'.",
            kind=UIEventKind.APPROVAL,
        )
        return ctx.effect.finish(control="continue")
    ctx.agent.session_approval_rules = remaining
    refresh_approval_runtime(
        ctx.agent, get_runtime_approval_config(ctx.config, ctx.agent)
    )

    view = _build_approval_view(ctx)
    ctx.effect.success(
        "Removed session approval rule",
        kind=UIEventKind.APPROVAL,
        target=command.target,
    )
    ctx.effect.refresh_view(
        view.view_type,
        title="Approval Rules",
        view_model=view,
        reuse_key="approval_rules",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_unset_global_approval_rule(command, ctx) -> CommandEffect:
    rule = _parse_unset_target(command, ctx)
    if rule is None:
        return ctx.effect.finish(control="continue")

    remaining = [
        existing
        for existing in ctx.config.approval.rules
        if not same_rule_target(existing, rule)
    ]
    if len(remaining) == len(ctx.config.approval.rules):
        ctx.effect.error(
            f"No global approval rule for '{command.target}'.",
            kind=UIEventKind.APPROVAL,
        )
        return ctx.effect.finish(control="continue")
    ctx.config.approval.rules = remaining
    path = WorkspaceConfigStore().save_approval_config(ctx.config.approval)
    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    refresh_approval_runtime(ctx.agent, approval)

    view = _build_approval_view(ctx)
    ctx.effect.success(
        f"Removed global approval rule and saved to {path}",
        kind=UIEventKind.APPROVAL,
        target=command.target,
        saved_path=str(path),
    )
    ctx.effect.refresh_view(
        view.view_type,
        title="Approval Rules",
        view_model=view,
        reuse_key="approval_rules",
    )
    return ctx.effect.finish(
        control="continue", state_changes={"saved_path": str(path), **view.to_payload()}
    )


def _handle_show_approval(command, ctx) -> CommandEffect:
    view = _build_approval_view(ctx)
    ctx.effect.open_view(
        view.view_type,
        title="Approval Rules",
        view_model=view,
        reuse_key="approval_rules",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _validate_approval_rule(command, ctx):
    if command.action not in VALID_APPROVAL_ACTIONS:
        ctx.effect.error(
            "approval action must be one of allow, warn, require_approval, deny",
            kind=UIEventKind.APPROVAL,
        )
        return None

    rule = parse_approval_target(command.target, command.action)
    if rule is None:
        ctx.effect.error(
            "target must be one of tool:<name>, mcp, mcp:<server>, or mcp:<server>:<tool>",
            kind=UIEventKind.APPROVAL,
        )
        return None
    return rule


def _handle_set_approval_rule(command, ctx) -> CommandEffect:
    rule = _validate_approval_rule(command, ctx)
    if rule is None:
        return ctx.effect.finish(control="continue")

    session_rules = list(getattr(ctx.agent, "session_approval_rules", []) or [])
    session_rules = [
        existing for existing in session_rules if not same_rule_target(existing, rule)
    ]
    session_rules.append(rule)
    ctx.agent.session_approval_rules = session_rules
    refresh_approval_runtime(
        ctx.agent, get_runtime_approval_config(ctx.config, ctx.agent)
    )

    view = _build_approval_view(ctx)
    ctx.effect.success(
        "Updated session approval rule",
        kind=UIEventKind.APPROVAL,
        target=command.target,
        action_name=command.action,
    )
    ctx.effect.refresh_view(
        view.view_type,
        title="Approval Rules",
        view_model=view,
        reuse_key="approval_rules",
    )

    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_set_global_approval_rule(command, ctx) -> CommandEffect:
    rule = _validate_approval_rule(command, ctx)
    if rule is None:
        return ctx.effect.finish(control="continue")

    ctx.config.approval.rules = [
        existing
        for existing in ctx.config.approval.rules
        if not same_rule_target(existing, rule)
    ]
    ctx.config.approval.rules.append(rule)
    path = WorkspaceConfigStore().save_approval_config(ctx.config.approval)
    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    refresh_approval_runtime(ctx.agent, approval)

    view = _build_approval_view(ctx)
    ctx.effect.success(
        f"Updated global approval rule and saved to {path}",
        kind=UIEventKind.APPROVAL,
        target=command.target,
        action_name=command.action,
        saved_path=str(path),
    )
    ctx.effect.refresh_view(
        view.view_type,
        title="Approval Rules",
        view_model=view,
        reuse_key="approval_rules",
    )

    return ctx.effect.finish(
        control="continue", state_changes={"saved_path": str(path), **view.to_payload()}
    )


def _approval_rule_target(rule: ApprovalRuleView) -> str:
    if rule.tool_source == "mcp":
        if rule.mcp_server and rule.tool_name:
            return f"mcp:{rule.mcp_server}:{rule.tool_name}"
        if rule.mcp_server:
            return f"mcp:{rule.mcp_server}"
        return "mcp"
    if rule.tool_name:
        return f"tool:{rule.tool_name}"
    return rule.scope


def _approval_action_items(
    target: str, prefix: str, current_action: str
) -> tuple[PanelItem, ...]:
    return tuple(
        PanelItem(
            label=action,
            description=f"/approval {prefix} {target} {action}",
            command=f"/approval {prefix} {target} {action}",
            current=action == current_action,
        )
        for action in ("allow", "warn", "require_approval", "deny")
    )


def command_panel_spec() -> CommandPanelSpec:
    """Contribute the approval target browser and action sub-panels."""

    def build(model: object, title: str) -> PanelDefinition | None:
        assert isinstance(model, ApprovalView)
        items: list[PanelItem] = []
        children: list[tuple[str, PanelDefinition]] = []
        covered: set[str] = set()
        for rule in model.rules:
            target = _approval_rule_target(rule)
            covered.add(target)
            prefix = "set" if rule.source == "session" else "set-global"
            actions = list(_approval_action_items(target, prefix, rule.action))
            if rule.source in ("session", "workspace", "global"):
                unset = "unset" if rule.source == "session" else "unset-global"
                actions.append(
                    PanelItem(
                        label="delete rule",
                        description=f"/approval {unset} {target}",
                        command=f"/approval {unset} {target}",
                    )
                )
            children.append(
                (
                    target,
                    PanelDefinition(
                        view_type="approval_actions",
                        title=f"{title} · {target}",
                        items=tuple(actions),
                    ),
                )
            )
            items.append(
                PanelItem(
                    label=target,
                    description=f"{rule.action} · {rule.source}",
                    command="",
                )
            )

        dynamic: list[tuple[PanelItem, PanelDefinition]] = []
        for policy in model.effective_mcp_policies:
            target = f"mcp:{policy.server_name}"
            if target in covered:
                continue
            dynamic.append(
                (
                    PanelItem(
                        label=target,
                        description=f"{policy.action} · effective (no rule)",
                        command="",
                    ),
                    PanelDefinition(
                        view_type="approval_actions",
                        title=f"{title} · {target}",
                        items=_approval_action_items(target, "set", policy.action),
                    ),
                )
            )
        for policy in model.tool_policies:
            if policy.tool_source != "builtin":
                continue
            target = f"tool:{policy.tool_name}"
            if target in covered:
                continue
            dynamic.append(
                (
                    PanelItem(
                        label=target,
                        description=f"{policy.action} · effective (no rule)",
                        command="",
                    ),
                    PanelDefinition(
                        view_type="approval_actions",
                        title=f"{title} · {target}",
                        items=_approval_action_items(target, "set", policy.action),
                    ),
                )
            )
        dynamic.sort(key=lambda entry: entry[0].label)
        items.extend(item for item, _child in dynamic)
        children.extend((item.label, child) for item, child in dynamic)
        if not items:
            return None
        return PanelDefinition(
            view_type=model.view_type,
            title=title,
            items=tuple(items),
            children=tuple(children),
        )

    return CommandPanelSpec(
        "approval_rules",
        ApprovalView,
        build,
        refresh=PanelRefreshPolicy.ABSORB,
    )


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
                during_turn=DuringTurnPolicy.IMMEDIATE,
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
                during_turn=DuringTurnPolicy.IMMEDIATE,
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
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="approval.unset",
                feature_id="approval",
                description="[session] Remove a session approval rule override",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval unset <target>"),),
                parser=_parse_unset_approval,
                handler=_handle_unset_approval_rule,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="approval.unset_global",
                feature_id="approval",
                description="[global] Remove a global approval rule default",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval unset-global <target>"),),
                parser=_parse_unset_global_approval,
                handler=_handle_unset_global_approval_rule,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
        ]
    )
