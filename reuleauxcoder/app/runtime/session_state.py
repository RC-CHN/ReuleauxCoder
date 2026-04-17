"""Helpers for session-scoped runtime state persistence and restore."""

from __future__ import annotations

from reuleauxcoder.app.runtime.approval import refresh_approval_runtime, same_rule_target
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig, Config
from reuleauxcoder.domain.session.models import Session, SessionRuntimeState
from reuleauxcoder.infrastructure.persistence.session_store import DEFAULT_SESSION_FINGERPRINT


def get_session_fingerprint(config: Config, agent: Agent) -> str:
    """Return the current session environment fingerprint."""
    return getattr(agent, "session_fingerprint", None) or getattr(
        config, "session_fingerprint", None
    ) or DEFAULT_SESSION_FINGERPRINT


def _clone_approval_rules(rules: list[ApprovalRuleConfig]) -> list[ApprovalRuleConfig]:
    return [
        ApprovalRuleConfig(
            tool_name=rule.tool_name,
            tool_source=rule.tool_source,
            mcp_server=rule.mcp_server,
            effect_class=rule.effect_class,
            profile=rule.profile,
            action=rule.action,
        )
        for rule in rules
    ]


def merge_approval_config(
    baseline: ApprovalConfig,
    session_rules: list[ApprovalRuleConfig] | None,
) -> ApprovalConfig:
    """Merge baseline approval config with session-scoped rule overrides."""
    merged_rules = _clone_approval_rules(baseline.rules)
    for rule in session_rules or []:
        merged_rules = [existing for existing in merged_rules if not same_rule_target(existing, rule)]
        merged_rules.append(
            ApprovalRuleConfig(
                tool_name=rule.tool_name,
                tool_source=rule.tool_source,
                mcp_server=rule.mcp_server,
                effect_class=rule.effect_class,
                profile=rule.profile,
                action=rule.action,
            )
        )
    return ApprovalConfig(default_mode=baseline.default_mode, rules=merged_rules)


def get_runtime_approval_config(config: Config, agent: Agent) -> ApprovalConfig:
    """Return the effective approval config from baseline + session rule overrides."""
    session_rules = getattr(agent, "session_approval_rules", None)
    return merge_approval_config(config.approval, session_rules)


def build_session_runtime_state(config: Config, agent: Agent) -> SessionRuntimeState:
    """Capture session-scoped runtime overrides from the live host runtime."""
    session_rules = getattr(agent, "session_approval_rules", None) or []
    return SessionRuntimeState(
        model=getattr(agent.llm, "model", None) or getattr(config, "model", None),
        active_mode=getattr(agent, "active_mode", None),
        llm_debug_trace=getattr(agent.llm, "debug_trace", None),
        active_main_model_profile=getattr(agent, "active_main_model_profile", None),
        active_sub_model_profile=getattr(agent, "active_sub_model_profile", None)
        or getattr(config, "active_sub_model_profile", None),
        approval_rules=[
            {
                "tool_name": rule.tool_name,
                "tool_source": rule.tool_source,
                "mcp_server": rule.mcp_server,
                "effect_class": rule.effect_class,
                "profile": rule.profile,
                "action": rule.action,
            }
            for rule in session_rules
        ],
    )


def restore_config_runtime_defaults(config: Config, agent: Agent) -> None:
    """Reset live runtime state back to config defaults for a fresh session."""
    profiles = getattr(config, "model_profiles", {}) or {}
    main_profile_name = getattr(config, "active_main_model_profile", None) or getattr(
        config, "active_model_profile", None
    )
    if main_profile_name and main_profile_name in profiles:
        profile = profiles[main_profile_name]
        agent.llm.reconfigure(
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            preserve_reasoning_content=profile.preserve_reasoning_content,
            backfill_reasoning_content_for_tool_calls=profile.backfill_reasoning_content_for_tool_calls,
            debug_trace=getattr(config, "llm_debug_trace", False),
        )
        agent.context.reconfigure(profile.max_context_tokens)
    else:
        agent.llm.debug_trace = getattr(config, "llm_debug_trace", False)
    setattr(agent, "active_main_model_profile", main_profile_name)
    setattr(agent, "active_sub_model_profile", getattr(config, "active_sub_model_profile", None))
    setattr(agent, "session_approval_rules", [])
    refresh_approval_runtime(agent, config.approval)

    default_mode = getattr(config, "active_mode", None)
    if default_mode and default_mode in getattr(agent, "available_modes", {}):
        agent.set_mode(default_mode)
    else:
        agent.active_mode = default_mode


def apply_session_runtime_state(session: Session, config: Config, agent: Agent) -> None:
    """Apply persisted session runtime state onto the live host runtime."""
    restore_config_runtime_defaults(config, agent)
    runtime = session.runtime_state

    agent.state.messages = list(session.messages)
    agent.state.total_prompt_tokens = session.total_prompt_tokens
    agent.state.total_completion_tokens = session.total_completion_tokens
    agent.state.current_round = 0

    loaded_mode = runtime.active_mode or session.active_mode
    if loaded_mode and loaded_mode in getattr(agent, "available_modes", {}):
        agent.set_mode(loaded_mode)

    loaded_debug = runtime.llm_debug_trace
    if loaded_debug is not None:
        agent.llm.debug_trace = loaded_debug

    if runtime.approval_rules:
        session_rules = [
            ApprovalRuleConfig(
                tool_name=rule.get("tool_name"),
                tool_source=rule.get("tool_source"),
                mcp_server=rule.get("mcp_server"),
                effect_class=rule.get("effect_class"),
                profile=rule.get("profile"),
                action=rule.get("action", config.approval.default_mode),
            )
            for rule in runtime.approval_rules
        ]
        setattr(agent, "session_approval_rules", session_rules)
        refresh_approval_runtime(agent, merge_approval_config(config.approval, session_rules))

    main_profile = runtime.active_main_model_profile
    profiles = getattr(config, "model_profiles", {}) or {}
    if main_profile and main_profile in profiles:
        profile = profiles[main_profile]
        agent.llm.reconfigure(
            model=profile.model,
            api_key=profile.api_key,
            base_url=profile.base_url,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            preserve_reasoning_content=profile.preserve_reasoning_content,
            backfill_reasoning_content_for_tool_calls=profile.backfill_reasoning_content_for_tool_calls,
            debug_trace=agent.llm.debug_trace,
        )
        agent.context.reconfigure(profile.max_context_tokens)
        setattr(agent, "active_main_model_profile", main_profile)
    elif runtime.model:
        agent.llm.model = runtime.model
        setattr(agent, "active_main_model_profile", None)

    setattr(agent, "active_sub_model_profile", runtime.active_sub_model_profile)
