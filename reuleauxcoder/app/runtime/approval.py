"""Shared approval runtime helpers and view builders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from reuleauxcoder.domain.approval_engine import (
    ApprovalPolicyEngine,
    ToolSource,
    ToolApprovalContext,
)
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalGrantCandidate,
    ApprovalRequest,
    SharedApprovalProvider,
)
from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.approval_review import AutoReviewJudge
from reuleauxcoder.domain.config.models import (
    ApprovalAction,
    ApprovalConfig,
    ApprovalRuleConfig,
)
from reuleauxcoder.domain.config.schema import DEFAULTS
from reuleauxcoder.domain.hooks import HookPoint
from reuleauxcoder.domain.hooks.builtin import ToolPolicyGuardHook
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.mcp.runtime import find_mcp_server
from reuleauxcoder.extensions.tools.builtin import builtin_tool_types
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader

VALID_APPROVAL_ACTIONS: frozenset[ApprovalAction] = frozenset(
    {"allow", "warn", "require_approval", "deny"}
)


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
    pattern: str | None = None
    scope_key: str | None = None
    source: str = "builtin"


@dataclass(slots=True)
class ApprovalEffectiveToolView:
    name: str
    action: str
    source: str


@dataclass(slots=True)
class ApprovalEffectivePolicyView:
    """Structured presentation model for one MCP server's effective policy."""

    server_name: str
    action: str
    source: str
    tools: list[ApprovalEffectiveToolView] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ApprovalEditorHint:
    supports_text_command: bool
    set_command_format: str
    future_ui_editor: bool
    targets: tuple[str, ...]


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
    editor_hint: ApprovalEditorHint = field(
        default_factory=lambda: ApprovalEditorHint(False, "", False, ())
    )
    view_type: str = "approval_rules"

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
                    "pattern": rule.pattern,
                    "scope_key": rule.scope_key,
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
                    "tools": [
                        {
                            "name": tool.name,
                            "action": tool.action,
                            "source": tool.source,
                        }
                        for tool in item.tools
                    ],
                }
                for item in self.effective_mcp_policies
            ],
            "editor_hint": {
                "supports_text_command": self.editor_hint.supports_text_command,
                "set_command_format": self.editor_hint.set_command_format,
                "future_ui_editor": self.editor_hint.future_ui_editor,
                "targets": list(self.editor_hint.targets),
            },
        }


def parse_approval_target(target: str, action: str) -> ApprovalRuleConfig | None:
    """Parse an approval target spec into a rule config."""
    if action not in VALID_APPROVAL_ACTIONS:
        return None
    normalized_action = cast(ApprovalAction, action)
    if target == "mcp":
        return ApprovalRuleConfig(tool_source="mcp", action=normalized_action)
    if target.startswith("tool:"):
        return ApprovalRuleConfig(tool_name=target[5:], action=normalized_action)
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
                action=normalized_action,
            )
        return ApprovalRuleConfig(
            tool_source="mcp", mcp_server=rest, action=normalized_action
        )
    return None


def same_rule_target(left: ApprovalRuleConfig, right: ApprovalRuleConfig) -> bool:
    """Return whether two rules target the same scope."""
    return (
        left.tool_name == right.tool_name
        and left.tool_source == right.tool_source
        and left.mcp_server == right.mcp_server
        and left.effect_class == right.effect_class
        and left.profile == right.profile
        and left.pattern == right.pattern
        and left.scope_key == right.scope_key
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
    for hook in agent.hook_registry.hooks_at(HookPoint.BEFORE_TOOL_EXECUTE):
        if isinstance(hook, ToolPolicyGuardHook):
            hook.update_approval_config(approval_config)


def clone_approval_rules(
    rules: Sequence[ApprovalRuleConfig],
) -> list[ApprovalRuleConfig]:
    """Detach mutable config rules before composing runtime policy."""
    return [
        ApprovalRuleConfig(
            tool_name=rule.tool_name,
            tool_source=rule.tool_source,
            mcp_server=rule.mcp_server,
            effect_class=rule.effect_class,
            profile=rule.profile,
            pattern=rule.pattern,
            scope_key=rule.scope_key,
            action=rule.action,
        )
        for rule in rules
    ]


def approval_rule_payload(rule: ApprovalRuleConfig) -> dict[str, Any]:
    """Serialize one rule for runtime events without coupling to YAML storage."""
    values = {
        "tool_name": rule.tool_name,
        "tool_source": rule.tool_source,
        "mcp_server": rule.mcp_server,
        "effect_class": rule.effect_class,
        "profile": rule.profile,
        "pattern": rule.pattern,
        "scope_key": rule.scope_key,
        "action": rule.action,
    }
    return {key: value for key, value in values.items() if value is not None}


def merge_approval_config(
    baseline: ApprovalConfig,
    session_rules: Sequence[ApprovalRuleConfig] | None,
) -> ApprovalConfig:
    """Layer session-scoped rule targets over baseline approval config."""
    merged_rules = clone_approval_rules(baseline.rules)
    for rule in session_rules or ():
        merged_rules = [
            existing
            for existing in merged_rules
            if not same_rule_target(existing, rule)
        ]
        merged_rules.extend(clone_approval_rules((rule,)))
    return ApprovalConfig(
        default_mode=baseline.default_mode,
        rules=merged_rules,
        reviewer=baseline.reviewer,
        auto_review_model_profile=baseline.auto_review_model_profile,
        auto_review_policy=baseline.auto_review_policy,
        auto_review_timeout_seconds=baseline.auto_review_timeout_seconds,
    )


def build_runtime_approval_provider(agent, handler) -> SharedApprovalProvider:
    """Build user or fail-closed auto-review routing from effective config."""

    approval = getattr(getattr(agent, "runtime_config", None), "approval", None)
    judges = [lambda request: _judge_session_approval_rules(agent, request)]
    reviewer = (
        "auto_review"
        if approval is not None
        and getattr(approval, "reviewer", "user") == "auto_review"
        else "user"
    )
    if reviewer == "auto_review":
        from reuleauxcoder.services.llm.factory import build_llm_from_settings

        profile_name = getattr(approval, "auto_review_model_profile", None)
        profiles = getattr(getattr(agent, "runtime_config", None), "model_profiles", {})
        profile = profiles.get(profile_name) if profile_name else None
        reviewer_llm = (
            build_llm_from_settings(
                profile, debug_trace=getattr(agent.llm, "debug_trace", False)
            )
            if profile is not None
            else None
        )
        judges.append(
            AutoReviewJudge(
                agent=agent,
                llm=reviewer_llm,
                policy=getattr(approval, "auto_review_policy", ""),
                timeout_seconds=getattr(approval, "auto_review_timeout_seconds", 15),
            )
        )
    return SharedApprovalProvider(
        handler=handler,
        judges=judges,
        reviewer=reviewer,
        on_request=lambda request: _record_approval_request(agent, request),
        on_decision=lambda request, decision: _record_approval_decision(
            agent, request, decision
        ),
        on_session_grant=lambda request, grant: apply_session_approval_grant(
            agent,
            request,
            grant,
        ),
    )


def _judge_session_approval_rules(
    agent,
    request: ApprovalRequest,
) -> ApprovalDecision | None:
    """Recheck live session rules at the provider boundary.

    Child agents may hold a cloned policy hook while a root-scoped approval is
    being granted. This narrow judge prevents a stale clone from prompting
    again; it evaluates session rules only and does not replace the ordinary
    authorization hook.
    """
    lock = getattr(agent, "_session_approval_lock", None)
    if lock is None:
        return None
    with lock:
        session_rules = clone_approval_rules(
            getattr(agent, "session_approval_rules", ()) or ()
        )
    if not session_rules:
        return None
    engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="require_approval",
            rules=session_rules,
        )
    )
    tool_source = (
        cast(ToolSource, request.tool_source)
        if request.tool_source in {"builtin", "mcp", "unknown"}
        else "unknown"
    )
    match = engine.evaluate(
        ToolApprovalContext(
            tool_call=ToolCall(
                id=request.request_id,
                name=request.tool_name,
                arguments=dict(request.tool_args),
            ),
            tool_name=request.tool_name,
            tool_source=tool_source,
            mcp_server=request.mcp_server,
            effect_class=request.effect_class,
            profile=request.profile,
            subjects=request.subjects,
            scope_key=request.scope_key,
        )
    )
    if match.rule is None:
        return None
    if match.action == "allow":
        return ApprovalDecision.allow_once("matched session approval grant")
    if match.action == "deny":
        return ApprovalDecision.deny_once("blocked by session approval rule")
    return None


def apply_session_approval_grant(
    agent,
    request: ApprovalRequest,
    grant: ApprovalGrantCandidate,
) -> None:
    """Atomically install a validated in-memory grant and refresh policy hooks."""
    if grant.scope_key != request.scope_key:
        raise ValueError("approval grant environment does not match the request")
    if not grant.proposed_rules:
        raise ValueError("approval grant contains no rules")
    if any(rule.action != "allow" for rule in grant.proposed_rules):
        raise ValueError("session approval grants may only contain allow rules")
    if any(rule.scope_key != request.scope_key for rule in grant.proposed_rules):
        raise ValueError("approval grant rule environment does not match the request")

    lock = getattr(agent, "_session_approval_lock", None)
    if lock is None:
        raise RuntimeError("agent has no session approval rule lock")
    with lock:
        rules = list(getattr(agent, "session_approval_rules", []) or [])
        for proposed in grant.proposed_rules:
            rules = [
                existing
                for existing in rules
                if not same_rule_target(existing, proposed)
            ]
            rules.append(proposed)
        agent.session_approval_rules = rules
        approval = merge_approval_config(
            agent.runtime_config.approval,
            agent.session_approval_rules,
        )
        refresh_approval_runtime(agent, approval)

    persist = getattr(agent, "persist_runtime_snapshot", None)
    if callable(persist):
        persist()


def _approval_identity(agent, request: ApprovalRequest) -> dict:
    metadata = request.metadata
    return {
        "agent_id": str(metadata.get("agent_id") or agent.agent_id),
        "session_generation": int(
            metadata.get("session_generation", agent.session_generation)
        ),
        "turn_id": metadata.get("turn_id"),
        "tool_call_id": metadata.get("tool_call_id"),
        "job_id": metadata.get("subagent_job_id"),
        "parent_agent_id": (agent.agent_id if metadata.get("is_subagent") else None),
    }


def _record_approval_request(agent, request: ApprovalRequest) -> None:
    identity = _approval_identity(agent, request)
    sections = [
        {
            "id": section.id,
            "title": section.title,
            "kind": section.kind.value,
            "content": section.content,
        }
        for section in (request.preview.sections if request.preview else ())
    ]
    agent.history_ledger.append(
        "approval_requested",
        {
            "request_id": request.request_id,
            "tool_name": request.tool_name,
            "tool_args": request.tool_args,
            "tool_source": request.tool_source,
            "mcp_server": request.mcp_server,
            "effect_class": request.effect_class,
            "profile": request.profile,
            "subjects": list(request.subjects),
            "scope_key": request.scope_key,
            "grant_candidates": [
                {
                    "id": candidate.id,
                    "label": candidate.label,
                    "description": candidate.description,
                    "broad": candidate.broad,
                    "rules": [
                        approval_rule_payload(rule)
                        for rule in candidate.proposed_rules
                    ],
                }
                for candidate in request.grant_candidates
            ],
            "reason": request.reason,
            "reviewer": request.metadata.get("reviewer"),
            "approval_attempt": request.metadata.get("approval_attempt"),
            "preview": sections,
            "metadata": dict(request.metadata),
        },
        agent_id=identity["agent_id"],
        parent_agent_id=identity["parent_agent_id"],
        job_id=identity["job_id"],
        turn_id=identity["turn_id"],
        api_round_id=(
            f"{identity['turn_id']}:{agent.state.current_round}"
            if identity["turn_id"]
            else None
        ),
    )
    agent.history_ledger.append(
        "attention_raised",
        {
            "attention_id": f"approval:{request.request_id}",
            "source_event": "approval_requested",
            "request_id": request.request_id,
        },
        agent_id=identity["agent_id"],
        parent_agent_id=identity["parent_agent_id"],
        job_id=identity["job_id"],
        turn_id=identity["turn_id"],
    )
    preview_text = sections[0]["title"] if sections else None
    event = AgentEvent.approval_requested(
        request_id=request.request_id,
        title=f"Approval required: {request.tool_name}",
        preview=preview_text,
    )
    event.agent_id = identity["agent_id"]
    event.session_generation = identity["session_generation"]
    event.turn_id = identity["turn_id"]
    agent._emit_event(event)
    agent.persist_runtime_snapshot()


def _record_approval_decision(
    agent, request: ApprovalRequest, decision: ApprovalDecision
) -> None:
    identity = _approval_identity(agent, request)
    agent.history_ledger.append(
        "approval_resolved",
        {
            "request_id": request.request_id,
            "approved": decision.approved,
            "mode": decision.mode,
            "reason": decision.reason,
            "reviewed": decision.reviewed,
            "reviewer": request.metadata.get("reviewer"),
            "approval_attempt": request.metadata.get("approval_attempt"),
            "grant_id": decision.grant.id if decision.grant is not None else None,
            "grant_label": (
                decision.grant.label if decision.grant is not None else None
            ),
            "released_request_ids": list(decision.released_request_ids),
        },
        agent_id=identity["agent_id"],
        parent_agent_id=identity["parent_agent_id"],
        job_id=identity["job_id"],
        turn_id=identity["turn_id"],
        api_round_id=(
            f"{identity['turn_id']}:{agent.state.current_round}"
            if identity["turn_id"]
            else None
        ),
    )
    agent.history_ledger.append(
        "attention_resolved",
        {
            "attention_id": f"approval:{request.request_id}",
            "source_event": "approval_resolved",
            "request_id": request.request_id,
            "approved": decision.approved,
        },
        agent_id=identity["agent_id"],
        parent_agent_id=identity["parent_agent_id"],
        job_id=identity["job_id"],
        turn_id=identity["turn_id"],
    )
    event = AgentEvent.approval_resolved(
        request_id=request.request_id,
        approved=decision.approved,
        reason=decision.reason,
    )
    event.agent_id = identity["agent_id"]
    event.session_generation = identity["session_generation"]
    event.turn_id = identity["turn_id"]
    agent._emit_event(event)
    agent.persist_runtime_snapshot()


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
        pattern=rule_dict.get("pattern"),
        scope_key=rule_dict.get("scope_key"),
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


def _build_tool_catalog(
    agent, builtin_tools: Sequence[object]
) -> list[tuple[str, str, str | None]]:
    """Collect visible tools as (name, tool_source, mcp_server)."""
    catalog: dict[tuple[str, str, str | None], None] = {}

    for tool in builtin_tools:
        tool_name = getattr(tool, "name", None)
        if not isinstance(tool_name, str) or not tool_name:
            continue
        tool_source = getattr(tool, "tool_source", None) or "builtin"
        if tool_source == "mcp":
            continue
        catalog[(tool_name, "builtin", None)] = None

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
        builtin_tools = builtin_tool_types()
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
        if rule.pattern:
            parts.append(f"pattern={rule.pattern}")
        if rule.scope_key:
            parts.append("runtime=session")
        visible_rules.append(
            ApprovalRuleView(
                scope=", ".join(parts) if parts else "<default match>",
                action=rule.action,
                tool_source=rule.tool_source,
                mcp_server=rule.mcp_server,
                tool_name=rule.tool_name,
                effect_class=rule.effect_class,
                profile=rule.profile,
                pattern=rule.pattern,
                scope_key=rule.scope_key,
                source=source,
            )
        )

    policy_engine = ApprovalPolicyEngine(config.approval)
    tool_policies: list[ApprovalToolPolicyView] = []
    for tool_name, tool_source, mcp_server in _build_tool_catalog(agent, builtin_tools):
        normalized_tool_source = (
            cast(ToolSource, tool_source)
            if tool_source in {"builtin", "mcp", "unknown"}
            else "unknown"
        )
        match = policy_engine.evaluate(
            ToolApprovalContext(
                tool_call=ToolCall(id="preview", name=tool_name, arguments={}),
                tool_name=tool_name,
                tool_source=normalized_tool_source,
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
                    ApprovalEffectiveToolView(
                        name=tool_name, action=tool_action, source=tool_source
                    )
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
        editor_hint=ApprovalEditorHint(
            supports_text_command=True,
            set_command_format=(
                "/approval set <target> <allow|warn|require_approval|deny>"
            ),
            future_ui_editor=True,
            targets=("tool:<name>", "mcp", "mcp:<server>", "mcp:<server>:<tool>"),
        ),
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
                    f"  - tool `{tool.name}` -> `{tool.action}` ({tool.source})"
                )
    return "\n".join(lines)
