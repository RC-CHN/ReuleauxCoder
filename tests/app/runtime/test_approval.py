from types import SimpleNamespace

from reuleauxcoder.app.runtime.approval import (
    find_matching_rule,
    is_disabled_mcp_rule,
    parse_approval_target,
    resolve_mcp_server_action,
    same_rule_target,
)
from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    MCPServerConfig,
)


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
