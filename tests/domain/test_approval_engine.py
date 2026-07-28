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
    subjects=(),
):
    return ToolApprovalContext(
        tool_call=ToolCall(id="1", name=tool_name, arguments={}),
        tool_name=tool_name,
        tool_source=tool_source,
        mcp_server=mcp_server,
        effect_class=effect_class,
        profile=profile,
        subjects=subjects,
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


def test_internal_read_is_deterministically_allowed_unless_rule_denies() -> None:
    default_engine = ApprovalPolicyEngine(
        ApprovalConfig(default_mode="require_approval")
    )
    denied_engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="require_approval",
            rules=[ApprovalRuleConfig(tool_name="history_read", action="deny")],
        )
    )
    context = _ctx(tool_name="history_read", effect_class="read_only_internal")

    assert default_engine.evaluate(context).action == "allow"
    assert denied_engine.evaluate(context).action == "deny"


def test_pattern_rule_does_not_match_a_call_without_subjects() -> None:
    engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="require_approval",
            rules=[
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="src/app.py",
                    action="allow",
                )
            ],
        )
    )

    match = engine.evaluate(_ctx(tool_name="edit_file"))

    assert match.action == "require_approval"
    assert match.rule is None


def test_exact_and_subtree_patterns_respect_path_boundaries() -> None:
    engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="require_approval",
            rules=[
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="src/app.py",
                    action="allow",
                ),
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="tests/**",
                    action="warn",
                ),
            ],
        )
    )

    assert (
        engine.evaluate(
            _ctx(tool_name="edit_file", subjects=("src/app.py",))
        ).action
        == "allow"
    )
    assert (
        engine.evaluate(
            _ctx(tool_name="edit_file", subjects=("src/app.py.bak",))
        ).action
        == "require_approval"
    )
    assert (
        engine.evaluate(
            _ctx(tool_name="edit_file", subjects=("tests",))
        ).action
        == "warn"
    )
    assert (
        engine.evaluate(
            _ctx(tool_name="edit_file", subjects=("tests/unit/test_app.py",))
        ).action
        == "warn"
    )
    assert (
        engine.evaluate(
            _ctx(tool_name="edit_file", subjects=("tests_extra/test_app.py",))
        ).action
        == "require_approval"
    )


def test_multiple_exact_rules_collectively_cover_a_multi_subject_call() -> None:
    engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="require_approval",
            rules=[
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="src/one.py",
                    action="allow",
                ),
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="src/two.py",
                    action="allow",
                ),
            ],
        )
    )

    covered = engine.evaluate(
        _ctx(
            tool_name="edit_file",
            subjects=("src/one.py", "src/two.py"),
        )
    )
    partial = engine.evaluate(
        _ctx(
            tool_name="edit_file",
            subjects=("src/one.py", "src/three.py"),
        )
    )

    assert covered.action == "allow"
    assert partial.action == "require_approval"
    assert partial.rule is None


def test_deny_for_any_subject_denies_the_whole_call() -> None:
    engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="allow",
            rules=[
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="secrets/**",
                    action="deny",
                )
            ],
        )
    )

    match = engine.evaluate(
        _ctx(
            tool_name="edit_file",
            subjects=("src/app.py", "secrets/key.txt"),
        )
    )

    assert match.action == "deny"
    assert match.rule is not None
    assert match.rule.pattern == "secrets/**"


def test_exact_pattern_is_more_specific_than_wildcard_for_same_tool() -> None:
    engine = ApprovalPolicyEngine(
        ApprovalConfig(
            default_mode="require_approval",
            rules=[
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="*",
                    action="deny",
                ),
                ApprovalRuleConfig(
                    tool_name="edit_file",
                    pattern="src/app.py",
                    action="allow",
                ),
            ],
        )
    )

    exact = engine.evaluate(
        _ctx(tool_name="edit_file", subjects=("src/app.py",))
    )
    other = engine.evaluate(
        _ctx(tool_name="edit_file", subjects=("src/other.py",))
    )

    assert exact.action == "allow"
    assert exact.rule is not None
    assert exact.rule.pattern == "src/app.py"
    assert other.action == "deny"
