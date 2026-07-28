"""Builtin approval command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass, replace
import shlex
from types import SimpleNamespace

from reuleauxcoder.app.commands.matchers import matches_any
from reuleauxcoder.app.commands.models import CommandEffect
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
    same_rule_policy_target,
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
    pattern: str | None = None


@dataclass(frozen=True, slots=True)
class SetGlobalApprovalRuleCommand:
    target: str
    action: str
    pattern: str | None = None


@dataclass(frozen=True, slots=True)
class UnsetApprovalRuleCommand:
    target: str
    pattern: str | None = None


@dataclass(frozen=True, slots=True)
class UnsetGlobalApprovalRuleCommand:
    target: str
    pattern: str | None = None


def _parse_show_approval(user_input: str, parse_ctx):
    if matches_any(user_input, ("/approval", "/approval show")):
        return EmptyCommand()
    return None


def _approval_tokens(user_input: str, verbs: frozenset[str]) -> list[str] | None:
    try:
        tokens = shlex.split(user_input)
    except ValueError:
        tokens = user_input.strip().split()
    if len(tokens) < 2 or tokens[0] != "/approval" or tokens[1] not in verbs:
        return None
    return tokens


def _parse_set_tokens(
    user_input: str,
    verbs: frozenset[str],
) -> tuple[str, str, str | None] | None:
    tokens = _approval_tokens(user_input, verbs)
    if tokens is None:
        return None
    if len(tokens) == 4:
        return tokens[2], tokens[3], None
    if len(tokens) == 5:
        return tokens[2], tokens[4], tokens[3]
    return "", "", None


def _parse_set_approval(user_input: str, parse_ctx):
    parsed = _parse_set_tokens(user_input, frozenset({"set"}))
    if parsed is None:
        return None
    target, action, pattern = parsed
    return SetApprovalRuleCommand(target=target, action=action, pattern=pattern)


def _parse_set_global_approval(user_input: str, parse_ctx):
    parsed = _parse_set_tokens(
        user_input,
        frozenset({"set-global", "set-workspace"}),
    )
    if parsed is None:
        return None
    target, action, pattern = parsed
    return SetGlobalApprovalRuleCommand(
        target=target,
        action=action,
        pattern=pattern,
    )


def _parse_unset_approval(user_input: str, parse_ctx):
    tokens = _approval_tokens(user_input, frozenset({"unset"}))
    if tokens is None:
        return None
    if len(tokens) == 3:
        return UnsetApprovalRuleCommand(target=tokens[2])
    if len(tokens) == 4:
        return UnsetApprovalRuleCommand(target=tokens[2], pattern=tokens[3])
    return UnsetApprovalRuleCommand(target="")


def _parse_unset_global_approval(user_input: str, parse_ctx):
    tokens = _approval_tokens(
        user_input,
        frozenset({"unset-global", "unset-workspace"}),
    )
    if tokens is None:
        return None
    if len(tokens) == 3:
        return UnsetGlobalApprovalRuleCommand(target=tokens[2])
    if len(tokens) == 4:
        return UnsetGlobalApprovalRuleCommand(
            target=tokens[2],
            pattern=tokens[3],
        )
    return UnsetGlobalApprovalRuleCommand(target="")


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
            "target must identify a tool, MCP server/tool, effect, or profile",
            kind=UIEventKind.APPROVAL,
        )
        return None
    # Reuse the target grammar without requiring an action.
    rule = parse_approval_target(
        command.target,
        "allow",
        pattern=command.pattern,
    )
    if rule is None:
        ctx.effect.error(
            "target must identify a tool, MCP server/tool, effect, or profile",
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
        existing
        for existing in session_rules
        if not same_rule_policy_target(existing, rule)
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
    _persist_session_approval_rules(ctx.agent)

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
        if not same_rule_policy_target(existing, rule)
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
        f"Removed workspace approval rule and saved to {path}",
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

    rule = parse_approval_target(
        command.target,
        command.action,
        pattern=command.pattern,
    )
    if rule is None:
        ctx.effect.error(
            "target must identify a tool, MCP server/tool, effect, or profile",
            kind=UIEventKind.APPROVAL,
        )
        return None
    return rule


def _handle_set_approval_rule(command, ctx) -> CommandEffect:
    rule = _validate_approval_rule(command, ctx)
    if rule is None:
        return ctx.effect.finish(control="continue")

    session_rules = list(getattr(ctx.agent, "session_approval_rules", []) or [])
    matched = False
    updated_rules = []
    for existing in session_rules:
        if same_rule_policy_target(existing, rule):
            updated_rules.append(replace(existing, action=rule.action))
            matched = True
        else:
            updated_rules.append(existing)
    if not matched:
        updated_rules.append(rule)
    session_rules = updated_rules
    ctx.agent.session_approval_rules = session_rules
    refresh_approval_runtime(
        ctx.agent, get_runtime_approval_config(ctx.config, ctx.agent)
    )
    _persist_session_approval_rules(ctx.agent)

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

    matched = False
    updated_rules = []
    for existing in ctx.config.approval.rules:
        if same_rule_policy_target(existing, rule):
            updated_rules.append(replace(existing, action=rule.action))
            matched = True
        else:
            updated_rules.append(existing)
    if not matched:
        updated_rules.append(rule)
    ctx.config.approval.rules = updated_rules
    path = WorkspaceConfigStore().save_approval_config(ctx.config.approval)
    approval = get_runtime_approval_config(ctx.config, ctx.agent)
    refresh_approval_runtime(ctx.agent, approval)

    view = _build_approval_view(ctx)
    ctx.effect.success(
        f"Updated workspace approval rule and saved to {path}",
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


def _persist_session_approval_rules(agent) -> None:
    persist = getattr(agent, "persist_runtime_snapshot", None)
    if callable(persist):
        persist()


def _approval_rule_selector(rule: ApprovalRuleView) -> str:
    dimensions = (
        ("source", rule.tool_source),
        ("mcp_server", rule.mcp_server),
        ("tool", rule.tool_name),
        ("effect", rule.effect_class),
        ("profile", rule.profile),
    )
    return ",".join(f"{key}={value}" for key, value in dimensions if value)


def _approval_target_label(rule: ApprovalRuleView) -> str:
    if rule.tool_source == "mcp" or rule.mcp_server:
        parts = ["MCP"]
        if rule.mcp_server:
            parts.append(rule.mcp_server)
        if rule.tool_name:
            parts.append(rule.tool_name)
        label = " · ".join(parts)
    elif rule.tool_name:
        label = rule.tool_name
    elif rule.effect_class:
        label = f"Effect · {rule.effect_class}"
    elif rule.profile:
        label = f"Profile · {rule.profile}"
    elif rule.tool_source:
        label = f"Source · {rule.tool_source}"
    else:
        label = rule.scope
    qualifiers = []
    if rule.effect_class and not label.startswith("Effect ·"):
        qualifiers.append(f"effect={rule.effect_class}")
    if rule.profile and not label.startswith("Profile ·"):
        qualifiers.append(f"profile={rule.profile}")
    if qualifiers:
        label += f" [{', '.join(qualifiers)}]"
    if rule.pattern:
        label += f" · {rule.pattern}"
    return label


def _approval_command(
    verb: str,
    selector: str,
    *,
    pattern: str | None = None,
    action: str | None = None,
) -> str:
    tokens = ["/approval", verb, selector]
    if pattern is not None:
        tokens.append(pattern)
    if action is not None:
        tokens.append(action)
    return shlex.join(tokens)


def _common_action(rules: list[ApprovalRuleView]) -> str | None:
    actions = {rule.action for rule in rules}
    return next(iter(actions)) if len(actions) == 1 else None


def _approval_action_items(
    selector: str,
    *,
    pattern: str | None,
    verb: str,
    unset_verb: str,
    current_action: str | None,
    can_remove: bool,
) -> tuple[PanelItem, ...]:
    labels = {
        "allow": ("Allow automatically", "Matching calls run without asking"),
        "warn": ("Warn, then run", "Show a warning but do not block execution"),
        "require_approval": ("Ask every time", "Require a review for every match"),
        "deny": ("Block", "Reject matching calls before execution"),
    }
    items = [
        PanelItem(
            label=labels[action][0],
            description=labels[action][1],
            command=_approval_command(
                verb,
                selector,
                pattern=pattern,
                action=action,
            ),
            current=action == current_action,
        )
        for action in ("allow", "warn", "require_approval", "deny")
    ]
    if can_remove:
        items.append(
            PanelItem(
                label="Remove this override",
                description="Fall back to the next matching policy",
                command=_approval_command(
                    unset_verb,
                    selector,
                    pattern=pattern,
                ),
            )
        )
    return tuple(items)


def _approval_lifetime_panel(
    *,
    title: str,
    selector: str,
    pattern: str | None,
    rules: list[ApprovalRuleView],
) -> PanelDefinition:
    session_rules = [rule for rule in rules if rule.source == "session"]
    workspace_rules = [rule for rule in rules if rule.source == "workspace"]
    inherited_rules = [
        rule for rule in rules if rule.source in {"global", "builtin", "effective"}
    ]
    persistent_inherited_rules = [
        rule for rule in inherited_rules if rule.source != "effective"
    ]
    session_action = _common_action(session_rules)
    workspace_action = _common_action(workspace_rules)
    inherited_action = _common_action(inherited_rules)

    def state(
        action: str | None,
        scoped_rules: list[ApprovalRuleView],
        *,
        empty: str,
    ) -> str:
        if action is not None:
            return f"currently {action}"
        if scoped_rules:
            return "multiple environment-bound values"
        return empty

    session_label = "This session"
    workspace_label = "This workspace"
    session_panel = PanelDefinition(
        view_type="approval_actions",
        title=f"{title} · This session",
        items=_approval_action_items(
            selector,
            pattern=pattern,
            verb="set",
            unset_verb="unset",
            current_action=session_action,
            can_remove=bool(session_rules),
        ),
    )
    workspace_panel = PanelDefinition(
        view_type="approval_actions",
        title=f"{title} · This workspace",
        items=_approval_action_items(
            selector,
            pattern=pattern,
            verb="set-workspace",
            unset_verb="unset-workspace",
            current_action=workspace_action,
            can_remove=bool(workspace_rules),
        ),
    )
    inherited = (
        f"inherits {inherited_action}" if inherited_action is not None else "no override"
    )
    return PanelDefinition(
        view_type="approval_lifetime",
        title=title,
        items=(
            PanelItem(
                label=session_label,
                description=state(
                    session_action,
                    session_rules,
                    empty="no override · stored with this conversation session",
                ),
                command="",
                current=bool(session_rules),
            ),
            PanelItem(
                label=workspace_label,
                description=state(
                    workspace_action,
                    workspace_rules,
                    empty=f"{inherited} · saved in .rcoder/config.yaml",
                ),
                command="",
                current=not session_rules
                and bool(workspace_rules or persistent_inherited_rules),
            ),
        ),
        children=(
            (session_label, session_panel),
            (workspace_label, workspace_panel),
        ),
    )


def command_panel_spec() -> CommandPanelSpec:
    """Contribute the approval target browser and action sub-panels."""

    def build(model: object, title: str) -> PanelDefinition | None:
        assert isinstance(model, ApprovalView)
        items: list[PanelItem] = []
        children: list[tuple[str, PanelDefinition]] = []
        grouped: dict[tuple[str, str | None], list[ApprovalRuleView]] = {}
        for rule in model.rules:
            selector = _approval_rule_selector(rule)
            if not selector:
                continue
            grouped.setdefault((selector, rule.pattern), []).append(rule)

        dynamic: list[ApprovalRuleView] = []
        for policy in model.effective_mcp_policies:
            dynamic.append(
                ApprovalRuleView(
                    scope=f"mcp_server={policy.server_name}",
                    action=policy.action,
                    tool_source="mcp",
                    mcp_server=policy.server_name,
                    source="effective",
                )
            )
        for policy in model.tool_policies:
            if policy.tool_source != "builtin":
                continue
            dynamic.append(
                ApprovalRuleView(
                    scope=f"tool={policy.tool_name}",
                    action=policy.action,
                    tool_source="builtin",
                    tool_name=policy.tool_name,
                    source="effective",
                )
            )

        for rule in dynamic:
            selector = _approval_rule_selector(rule)
            key = (selector, None)
            if key not in grouped:
                grouped[key] = [rule]

        used_labels: set[str] = set()
        source_priority = {
            "session": 0,
            "workspace": 1,
            "global": 2,
            "builtin": 3,
            "effective": 4,
        }
        for (selector, pattern), rules in sorted(
            grouped.items(),
            key=lambda entry: (
                min(source_priority.get(rule.source, 5) for rule in entry[1]),
                _approval_target_label(entry[1][0]).lower(),
            ),
        ):
            label = _approval_target_label(rules[0])
            if label in used_labels:
                label = f"{label} · {selector}"
            used_labels.add(label)
            configured = [rule for rule in rules if rule.source != "effective"]
            if configured:
                description = " · ".join(
                    f"{rule.source}: {rule.action}" for rule in configured
                )
            else:
                description = f"effective: {rules[0].action} · no override"
            items.append(PanelItem(label=label, description=description, command=""))
            children.append(
                (
                    label,
                    _approval_lifetime_panel(
                        title=f"{title} · {label}",
                        selector=selector,
                        pattern=pattern,
                        rules=rules,
                    ),
                )
            )
        if not items:
            return None
        return PanelDefinition(
            view_type=model.view_type,
            title=title,
            items=tuple(items),
            children=tuple(children),
            filterable=True,
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
                description="[session] Set a conversation-session approval override",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/approval set <target> [pattern] <action>"),
                ),
                parser=_parse_set_approval,
                handler=_handle_set_approval_rule,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="approval.set_global",
                feature_id="approval",
                description="[workspace] Set a persistent workspace approval override",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger(
                        "/approval set-workspace <target> [pattern] <action>"
                    ),
                    slash_trigger(
                        "/approval set-global <target> [pattern] <action>"
                    ),
                ),
                parser=_parse_set_global_approval,
                handler=_handle_set_global_approval_rule,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="approval.unset",
                feature_id="approval",
                description="[session] Remove a conversation-session override",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/approval unset <target> [pattern]"),),
                parser=_parse_unset_approval,
                handler=_handle_unset_approval_rule,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="approval.unset_global",
                feature_id="approval",
                description="[workspace] Remove a persistent workspace override",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/approval unset-workspace <target> [pattern]"),
                    slash_trigger("/approval unset-global <target> [pattern]"),
                ),
                parser=_parse_unset_global_approval,
                handler=_handle_unset_global_approval_rule,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
        ]
    )
