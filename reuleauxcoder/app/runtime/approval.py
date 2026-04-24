"""Shared approval runtime helpers and view builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.approval_engine import (
    ApprovalPolicyEngine,
    ToolApprovalContext,
)
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.domain.config.schema import DEFAULTS
from reuleauxcoder.domain.hooks import HookPoint
from reuleauxcoder.domain.hooks.builtin import ToolPolicyGuardHook
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.mcp.runtime import find_mcp_server
from reuleauxcoder.extensions.tools.registry import build_tools
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader

VALID_APPROVAL_ACTIONS = {"allow", "warn", "require_approval", "deny"}


@dataclass(slots=True)
class ApprovalRuleView:
    """Structured presentation model for one configured approval rule."""

    scope: str
    action: str
    tool_source: str | None = None
    mcp_server: str | None = None
    tool_name: str | None = None
    effect_class: str | None = None
    profile: str | None = None
    source: str = "builtin"


@dataclass(slots=True)
class ApprovalEffectivePolicyView:
    """Structured presentation model for one MCP server's effective policy."""

    server_name: str
    action: str
    source: str
    tools: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ApprovalToolPolicyView:
    """Effective approval policy for a single callable tool."""

    tool_name: str
    action: str
    source: str
    tool_source: str
    scope: str


@dataclass(slots=True)
class ApprovalView:
    """Structured approval rules view payload."""

    default_mode: str
    default_mode_source: str = "builtin"
    rules: list[ApprovalRuleView] = field(default_factory=list)
    tool_policies: list[ApprovalToolPolicyView] = field(default_factory=list)
    effective_mcp_policies: list[ApprovalEffectivePolicyView] = field(
        default_factory=list
    )
    editor_hint: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "default_mode": self.default_mode,
            "default_mode_source": self.default_mode_source,
            "rules": [
                {
                    "scope": rule.scope,
                    "action": rule.action,
                    "tool_source": rule.tool_source,
                    "mcp_server": rule.mcp_server,
                    "tool_name": rule.tool_name,
                    "effect_class": rule.effect_class,
                    "profile": rule.profile,
                    "source": rule.source,
                }
                for rule in self.rules
            ],
            "tool_policies": [
                {
                    "tool_name": policy.tool_name,
                    "action": policy.action,
                    "source": policy.source,
                    "tool_source": policy.tool_source,
                    "scope": policy.scope,
                }
                for policy in self.tool_policies
            ],
            "effective_mcp_policies": [
                {
                    "server_name": item.server_name,
                    "action": item.action,
                    "source": item.source,
                    "tools": item.tools,
                }
                for item in self.effective_mcp_policies
            ],
            "editor_hint": self.editor_hint,
            "markdown": build_approval_markdown(self),
        }


def parse_approval_target(target: str, action: str) -> ApprovalRuleConfig | None:
    """Parse an approval target spec into a rule config."""
    if action not in VALID_APPROVAL_ACTIONS:
        return None
    if target == "mcp":
        return ApprovalRuleConfig(tool_source="mcp", action=action)
    if target.startswith("tool:"):
        return ApprovalRuleConfig(tool_name=target[5:], action=action)
    if target.startswith("mcp:"):
        rest = target[4:]
        if not rest:
            return None
        if ":" in rest:
            server, tool_name = rest.split(":", 1)
            if not server or not tool_name:
                return None
            return ApprovalRuleConfig(
                tool_source="mcp",
                mcp_server=server,
                tool_name=tool_name,
                action=action,
            )
        return ApprovalRuleConfig(tool_source="mcp", mcp_server=rest, action=action)
    return None


def same_rule_target(left: ApprovalRuleConfig, right: ApprovalRuleConfig) -> bool:
    """Return whether two rules target the same scope."""
    return (
        left.tool_name == right.tool_name
        and left.tool_source == right.tool_source
        and left.mcp_server == right.mcp_server
        and left.effect_class == right.effect_class
        and left.profile == right.profile
    )


def find_matching_rule(
    rules: list[ApprovalRuleConfig], target: ApprovalRuleConfig
) -> ApprovalRuleConfig | None:
    """Find an exactly matching rule target."""
    for rule in rules:
        if same_rule_target(rule, target):
            return rule
    return None


def resolve_mcp_server_action(config, server_name: str) -> str:
    """Resolve the effective approval action for an MCP server."""
    tool_rule = find_matching_rule(
        config.approval.rules,
        ApprovalRuleConfig(
            tool_source="mcp",
            mcp_server=server_name,
            action=config.approval.default_mode,
        ),
    )
    if tool_rule is not None:
        return tool_rule.action
    generic_rule = find_matching_rule(
        config.approval.rules,
        ApprovalRuleConfig(tool_source="mcp", action=config.approval.default_mode),
    )
    if generic_rule is not None:
        return generic_rule.action
    return config.approval.default_mode


def refresh_approval_runtime(agent, approval_config: ApprovalConfig) -> None:
    """Push approval config changes into live runtime hooks."""
    hooks = agent.hook_registry._hooks.get(HookPoint.BEFORE_TOOL_EXECUTE, [])
    for hook in hooks:
        if isinstance(hook, ToolPolicyGuardHook):
            hook.update_approval_config(approval_config)


def is_disabled_mcp_rule(config, rule: ApprovalRuleConfig) -> bool:
    """Return whether a rule targets a disabled MCP server and should be hidden."""
    if rule.tool_source != "mcp" or not rule.mcp_server:
        return False
    server = find_mcp_server(getattr(config, "mcp_servers", []), rule.mcp_server)
    if server is None:
        return False
    return not bool(getattr(server, "enabled", True))


def _load_raw_approval(path) -> dict:
    """Load raw ``approval`` section from a YAML config file."""
    if not path.exists():
        return {}
    try:
        data = load_yaml_config(path)
        return data.get("approval") or {}
    except Exception:
        return {}


def _raw_rule_to_config(rule_dict: dict) -> ApprovalRuleConfig:
    """Convert a raw YAML approval rule dict into an ApprovalRuleConfig."""
    return ApprovalRuleConfig(
        tool_name=rule_dict.get("tool_name"),
        tool_source=rule_dict.get("tool_source"),
        mcp_server=rule_dict.get("mcp_server"),
        effect_class=rule_dict.get("effect_class"),
        profile=rule_dict.get("profile"),
        action=rule_dict.get("action", "require_approval"),
    )


def _has_rule_in_list(
    rule: ApprovalRuleConfig, rules: list[ApprovalRuleConfig]
) -> bool:
    """Return whether an equivalent rule target exists in the given list."""
    for r in rules:
        if same_rule_target(rule, r):
            return True
    return False


def _resolve_rule_source(
    rule: ApprovalRuleConfig,
    *,
    session_rules: list[ApprovalRuleConfig],
    workspace_rules: list[ApprovalRuleConfig],
    global_rules: list[ApprovalRuleConfig],
    builtin_rules: list[ApprovalRuleConfig],
) -> str:
    """Resolve where a concrete runtime rule originated from."""
    if _has_rule_in_list(rule, session_rules):
        return "session"
    if _has_rule_in_list(rule, workspace_rules):
        return "workspace"
    if _has_rule_in_list(rule, global_rules):
        return "global"
    if _has_rule_in_list(rule, builtin_rules):
        return "builtin"
    return "workspace"


def _build_tool_catalog(agent, builtin_tools: list) -> list[tuple[str, str, str | None]]:
    """Collect visible tools as (name, tool_source, mcp_server)."""
    catalog: dict[tuple[str, str, str | None], None] = {}

    for tool in builtin_tools:
        tool_source = getattr(tool, "tool_source", None) or "builtin"
        if tool_source == "mcp":
            continue
        catalog[(tool.name, "builtin", None)] = None

    if agent is None:
        return sorted(catalog.keys(), key=lambda item: item[0])

    for tool in getattr(agent, "tools", []):
        tool_source = getattr(tool, "tool_source", None) or "builtin"
        server_name = (
            getattr(tool, "server_name", None) if tool_source == "mcp" else None
        )
        catalog[(tool.name, tool_source, server_name)] = None

    return sorted(catalog.keys(), key=lambda item: (item[1], item[2] or "", item[0]))


def build_approval_view(config, agent=None, builtin_tools=None) -> ApprovalView:
    """Build a structured view for approval rules and effective tool policies."""
    if builtin_tools is None:
        builtin_tools = build_tools()
    session_rules = (
        list(getattr(agent, "session_approval_rules", []) or [])
        if agent is not None
        else []
    )
    workspace_raw = _load_raw_approval(ConfigLoader.WORKSPACE_CONFIG_PATH)
    global_raw = _load_raw_approval(ConfigLoader.GLOBAL_CONFIG_PATH)

    builtin_rules = [_raw_rule_to_config(r) for r in DEFAULTS.get("approval_rules", [])]
    workspace_rules = [_raw_rule_to_config(r) for r in workspace_raw.get("rules", [])]
    global_rules = [_raw_rule_to_config(r) for r in global_raw.get("rules", [])]

    if workspace_raw and "default_mode" in workspace_raw:
        default_mode_source = "workspace"
    elif global_raw and "default_mode" in global_raw:
        default_mode_source = "global"
    else:
        default_mode_source = "builtin"

    visible_rules: list[ApprovalRuleView] = []
    for rule in config.approval.rules:
        if is_disabled_mcp_rule(config, rule):
            continue
        source = _resolve_rule_source(
            rule,
            session_rules=session_rules,
            workspace_rules=workspace_rules,
            global_rules=global_rules,
            builtin_rules=builtin_rules,
        )
        parts = []
        if rule.tool_source:
            parts.append(f"source={rule.tool_source}")
        if rule.mcp_server:
            parts.append(f"mcp_server={rule.mcp_server}")
        if rule.tool_name:
            parts.append(f"tool={rule.tool_name}")
        if rule.effect_class:
            parts.append(f"effect={rule.effect_class}")
        if rule.profile:
            parts.append(f"profile={rule.profile}")
        visible_rules.append(
            ApprovalRuleView(
                scope=", ".join(parts) if parts else "<default match>",
                action=rule.action,
                tool_source=rule.tool_source,
                mcp_server=rule.mcp_server,
                tool_name=rule.tool_name,
                effect_class=rule.effect_class,
                profile=rule.profile,
                source=source,
            )
        )

    policy_engine = ApprovalPolicyEngine(config.approval)
    tool_policies: list[ApprovalToolPolicyView] = []
    for tool_name, tool_source, mcp_server in _build_tool_catalog(agent, builtin_tools):
        match = policy_engine.evaluate(
            ToolApprovalContext(
                tool_call=ToolCall(id="preview", name=tool_name, arguments={}),
                tool_name=tool_name,
                tool_source=tool_source
                if tool_source in {"builtin", "mcp", "unknown"}
                else "unknown",
                mcp_server=mcp_server,
            )
        )
        if match.rule is None:
            source = default_mode_source
            scope = "<default_mode>"
        else:
            source = _resolve_rule_source(
                match.rule,
                session_rules=session_rules,
                workspace_rules=workspace_rules,
                global_rules=global_rules,
                builtin_rules=builtin_rules,
            )
            parts = []
            if match.rule.tool_source:
                parts.append(f"source={match.rule.tool_source}")
            if match.rule.mcp_server:
                parts.append(f"mcp_server={match.rule.mcp_server}")
            if match.rule.tool_name:
                parts.append(f"tool={match.rule.tool_name}")
            if match.rule.effect_class:
                parts.append(f"effect={match.rule.effect_class}")
            if match.rule.profile:
                parts.append(f"profile={match.rule.profile}")
            scope = ", ".join(parts) if parts else "<default match>"
        tool_policies.append(
            ApprovalToolPolicyView(
                tool_name=tool_name,
                action=match.action,
                source=source,
                tool_source=tool_source,
                scope=scope,
            )
        )

    effective_policies: list[ApprovalEffectivePolicyView] = []
    if agent is not None:
        mcp_tools_by_server: dict[str, list[str]] = {}
        for tool in getattr(agent, "tools", []):
            if getattr(tool, "tool_source", None) != "mcp":
                continue
            server_name = getattr(tool, "server_name", None) or "unknown"
            mcp_tools_by_server.setdefault(server_name, []).append(tool.name)

        for server_name in sorted(mcp_tools_by_server):
            server_rule = find_matching_rule(
                config.approval.rules,
                ApprovalRuleConfig(
                    tool_source="mcp",
                    mcp_server=server_name,
                    action=config.approval.default_mode,
                ),
            )
            server_action = (
                server_rule.action
                if server_rule is not None
                else resolve_mcp_server_action(config, server_name)
            )
            server_source = (
                "configured at server level"
                if server_rule is not None
                else "inherited from generic mcp/default"
            )
            tools = []
            for tool_name in sorted(mcp_tools_by_server[server_name]):
                tool_rule = find_matching_rule(
                    config.approval.rules,
                    ApprovalRuleConfig(
                        tool_source="mcp",
                        mcp_server=server_name,
                        tool_name=tool_name,
                        action=config.approval.default_mode,
                    ),
                )
                tool_action = (
                    tool_rule.action if tool_rule is not None else server_action
                )
                tool_source = (
                    "configured at tool level"
                    if tool_rule is not None
                    else f"inherited from server {server_name}"
                )
                tools.append(
                    {"name": tool_name, "action": tool_action, "source": tool_source}
                )
            effective_policies.append(
                ApprovalEffectivePolicyView(
                    server_name=server_name,
                    action=server_action,
                    source=server_source,
                    tools=tools,
                )
            )

    return ApprovalView(
        default_mode=config.approval.default_mode,
        default_mode_source=default_mode_source,
        rules=visible_rules,
        tool_policies=tool_policies,
        effective_mcp_policies=effective_policies,
        editor_hint={
            "supports_text_command": True,
            "set_command_format": "/approval set <target> <allow|warn|require_approval|deny>",
            "future_ui_editor": True,
            "targets": ["tool:<name>", "mcp", "mcp:<server>", "mcp:<server>:<tool>"],
        },
    )


def build_approval_markdown(view: ApprovalView) -> str:
    """Render a readable markdown summary for CLI/text UIs."""
    lines = [
        f"**Approval default_mode:** `{view.default_mode}` _(source: {view.default_mode_source})_",
        "",
    ]
    if not view.rules:
        lines.append("> No approval rules configured.")
    else:
        lines.append("**Configured rules:**")
        lines.append("")
        for idx, rule in enumerate(view.rules, 1):
            lines.append(
                f"{idx}. `{rule.scope}` -> **{rule.action}** _(source: {rule.source})_"
            )
    if view.tool_policies:
        lines.append("")
        lines.append("**Effective tool policies (including implicit/default):**")
        lines.append("")
        for idx, policy in enumerate(view.tool_policies, 1):
            lines.append(
                f"{idx}. `{policy.tool_name}` [{policy.tool_source}] -> **{policy.action}** "
                f"_(source: {policy.source}; matched: {policy.scope})_"
            )
    if view.effective_mcp_policies:
        lines.append("")
        lines.append("**MCP effective policy view:**")
        lines.append("")
        for item in view.effective_mcp_policies:
            lines.append(f"- **{item.server_name}** -> `{item.action}`")
            lines.append(f"  - source: {item.source}")
            for tool in item.tools:
                lines.append(
                    f"  - tool `{tool['name']}` -> `{tool['action']}` ({tool['source']})"
                )
    return "\n".join(lines)
