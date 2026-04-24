from reuleauxcoder.domain.approval_engine import (
    ApprovalPolicyEngine,
    ToolApprovalContext,
)
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.domain.llm.models import ToolCall


def _ctx(
    *,
    tool_name: str = "shell",
    tool_source: str = "builtin",
    mcp_server=None,
    effect_class=None,
    profile=None,
):
    return ToolApprovalContext(
        tool_call=ToolCall(id="1", name=tool_name, arguments={}),
        tool_name=tool_name,
        tool_source=tool_source,
        mcp_server=mcp_server,
        effect_class=effect_class,
        profile=profile,
    )


def test_approval_engine_returns_default_when_no_rule_matches() -> None:
    engine = ApprovalPolicyEngine(ApprovalConfig(default_mode="warn"))
    match = engine.evaluate(_ctx())
    assert match.action == "warn"
    assert match.rule is None


def test_approval_engine_prefers_more_specific_rule() -> None:
    config = ApprovalConfig(
        default_mode="require_approval",
        rules=[
            ApprovalRuleConfig(tool_source="mcp", action="warn"),
            ApprovalRuleConfig(tool_source="mcp", mcp_server="server-1", action="deny"),
            ApprovalRuleConfig(
                tool_source="mcp",
                mcp_server="server-1",
                tool_name="search",
                action="allow",
            ),
        ],
    )
    engine = ApprovalPolicyEngine(config)

    match = engine.evaluate(
        _ctx(tool_name="search", tool_source="mcp", mcp_server="server-1")
    )

    assert match.action == "allow"
    assert match.rule is not None
    assert match.rule.tool_name == "search"


def test_approval_engine_matches_profile_and_effect_class() -> None:
    config = ApprovalConfig(
        default_mode="require_approval",
        rules=[
            ApprovalRuleConfig(
                tool_name="shell",
                profile="coder",
                effect_class="filesystem_write",
                action="deny",
            )
        ],
    )
    engine = ApprovalPolicyEngine(config)

    deny_match = engine.evaluate(_ctx(effect_class="filesystem_write", profile="coder"))
    default_match = engine.evaluate(
        _ctx(effect_class="filesystem_read", profile="coder")
    )

    assert deny_match.action == "deny"
    assert default_match.action == "require_approval"


def test_approval_engine_specificity_scoring_orders_narrower_rules_higher() -> None:
    generic = ApprovalRuleConfig(tool_source="mcp", action="warn")
    server = ApprovalRuleConfig(tool_source="mcp", mcp_server="s1", action="deny")
    tool = ApprovalRuleConfig(
        tool_source="mcp", mcp_server="s1", tool_name="search", action="allow"
    )

    assert ApprovalPolicyEngine._specificity(
        generic
    ) < ApprovalPolicyEngine._specificity(server)
    assert ApprovalPolicyEngine._specificity(
        server
    ) < ApprovalPolicyEngine._specificity(tool)
