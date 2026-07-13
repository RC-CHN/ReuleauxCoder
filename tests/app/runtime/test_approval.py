from types import SimpleNamespace

from reuleauxcoder.app.runtime.approval import (
    build_runtime_approval_provider,
    find_matching_rule,
    is_disabled_mcp_rule,
    parse_approval_target,
    refresh_approval_runtime,
    resolve_mcp_server_action,
    same_rule_target,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalRequest
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    MCPServerConfig,
)
from reuleauxcoder.domain.hooks import HookPoint, HookRegistry
from reuleauxcoder.domain.hooks.builtin import ToolPolicyGuardHook


class _LLM:
    model = "model"


def _approval_agent() -> Agent:
    agent = Agent(llm=_LLM(), tools=[])
    agent.runtime_config = SimpleNamespace(
        approval=ApprovalConfig(reviewer="user"), model_profiles={}
    )
    agent.current_session_id = "session"
    agent.history_ledger.bind_context(session_id="session", agent_id=agent.agent_id)
    return agent


def test_parse_approval_target_supports_tool_and_mcp_targets() -> None:
    tool_rule = parse_approval_target("tool:shell", "deny")
    mcp_rule = parse_approval_target("mcp:server1:search", "warn")
    generic_mcp_rule = parse_approval_target("mcp", "allow")

    assert tool_rule is not None
    assert tool_rule.tool_name == "shell"
    assert tool_rule.action == "deny"

    assert mcp_rule is not None
    assert mcp_rule.tool_source == "mcp"
    assert mcp_rule.mcp_server == "server1"
    assert mcp_rule.tool_name == "search"
    assert mcp_rule.action == "warn"

    assert generic_mcp_rule is not None
    assert generic_mcp_rule.tool_source == "mcp"
    assert generic_mcp_rule.tool_name is None


def test_parse_approval_target_rejects_invalid_target_or_action() -> None:
    assert parse_approval_target("unknown", "allow") is None
    assert parse_approval_target("mcp:", "allow") is None
    assert parse_approval_target("mcp:server:", "allow") is None
    assert parse_approval_target("tool:shell", "invalid") is None


def test_same_rule_target_and_find_matching_rule() -> None:
    left = ApprovalRuleConfig(
        tool_source="mcp", mcp_server="s1", tool_name="search", action="allow"
    )
    right = ApprovalRuleConfig(
        tool_source="mcp", mcp_server="s1", tool_name="search", action="deny"
    )
    other = ApprovalRuleConfig(
        tool_source="mcp", mcp_server="s2", tool_name="search", action="allow"
    )

    assert same_rule_target(left, right) is True
    assert same_rule_target(left, other) is False
    assert find_matching_rule([other, right], left) is right


def test_resolve_mcp_server_action_prefers_server_rule_then_generic_then_default() -> (
    None
):
    config = SimpleNamespace(
        approval=ApprovalConfig(
            default_mode="require_approval",
            rules=[
                ApprovalRuleConfig(tool_source="mcp", action="warn"),
                ApprovalRuleConfig(
                    tool_source="mcp", mcp_server="server1", action="deny"
                ),
            ],
        )
    )

    assert resolve_mcp_server_action(config, "server1") == "deny"
    assert resolve_mcp_server_action(config, "server2") == "warn"


def test_is_disabled_mcp_rule_checks_server_enabled_flag() -> None:
    config = SimpleNamespace(
        mcp_servers=[MCPServerConfig(name="server1", command="cmd", enabled=False)]
    )
    rule = ApprovalRuleConfig(tool_source="mcp", mcp_server="server1", action="deny")
    non_mcp_rule = ApprovalRuleConfig(tool_name="shell", action="deny")

    assert is_disabled_mcp_rule(config, rule) is True
    assert is_disabled_mcp_rule(config, non_mcp_rule) is False


def test_refresh_approval_runtime_uses_public_registry_view() -> None:
    registry = HookRegistry()
    hook = ToolPolicyGuardHook(approval_config=ApprovalConfig(default_mode="deny"))
    registry.register(HookPoint.BEFORE_TOOL_EXECUTE, hook)
    agent = SimpleNamespace(hook_registry=registry)

    refresh_approval_runtime(agent, ApprovalConfig(default_mode="allow"))

    assert hook.approval_engine is not None
    assert hook.approval_engine.config.default_mode == "allow"


def test_runtime_approval_is_ledgered_and_emitted_before_resolution() -> None:
    agent = _approval_agent()
    events = []
    agent.add_event_handler(events.append)

    def handler(pending) -> None:
        assert any(
            event.kind == "approval_requested" for event in agent.history_ledger.events
        )
        pending.resolve(ApprovalDecision.allow_once("approved", reviewed=True))

    provider = build_runtime_approval_provider(agent, handler)
    request = ApprovalRequest(
        tool_name="edit_file",
        metadata={
            "agent_id": agent.agent_id,
            "session_generation": agent.session_generation,
            "turn_id": "turn",
            "tool_call_id": "call",
            "approval_attempt": 0,
        },
    )
    decision = provider.request_approval(request)

    assert decision.approved
    assert [event.kind for event in agent.history_ledger.events] == [
        "approval_requested",
        "attention_raised",
        "approval_resolved",
        "attention_resolved",
    ]
    assert [event.event_type for event in events] == [
        AgentEventType.APPROVAL_REQUESTED,
        AgentEventType.APPROVAL_RESOLVED,
    ]
    resolved = next(
        event
        for event in agent.history_ledger.events
        if event.kind == "approval_resolved"
    )
    assert resolved.payload["reason"] == "approved"


def test_bubbled_approval_keeps_child_attribution_in_root_ledger() -> None:
    agent = _approval_agent()
    provider = build_runtime_approval_provider(
        agent,
        lambda pending: pending.resolve(ApprovalDecision.deny_once("no")),
    )
    provider.request_approval(
        ApprovalRequest(
            tool_name="shell",
            metadata={
                "agent_id": "child",
                "session_generation": 3,
                "turn_id": "child-turn",
                "tool_call_id": "child-call",
                "is_subagent": True,
                "subagent_job_id": "sj_1",
            },
        )
    )
    requested = next(
        event
        for event in agent.history_ledger.events
        if event.kind == "approval_requested"
    )
    resolved = next(
        event
        for event in agent.history_ledger.events
        if event.kind == "approval_resolved"
    )
    assert requested.agent_id == resolved.agent_id == "child"
    assert requested.parent_agent_id == agent.agent_id
    assert requested.job_id == "sj_1"
    assert resolved.payload["reason"] == "no"
