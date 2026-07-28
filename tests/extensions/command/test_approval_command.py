from types import SimpleNamespace
from reuleauxcoder.app.commands.models import CommandEffect

from reuleauxcoder.domain.config.models import (
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
)
from reuleauxcoder.domain.hooks import HookPoint
from reuleauxcoder.domain.hooks.builtin import ToolPolicyGuardHook
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.extensions.command.builtin.approval import (
    SetApprovalRuleCommand,
    SetGlobalApprovalRuleCommand,
    UnsetApprovalRuleCommand,
    UnsetGlobalApprovalRuleCommand,
    _handle_set_approval_rule,
    _handle_set_global_approval_rule,
    _handle_unset_approval_rule,
    _handle_unset_global_approval_rule,
    _parse_set_approval,
    _parse_set_global_approval,
    _parse_unset_approval,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


def _build_ctx() -> SimpleNamespace:
    config = Config(api_key="key", approval=ApprovalConfig())
    hook_registry = HookRegistry()
    hook_registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        ToolPolicyGuardHook.create_from_config(config),
    )
    agent = SimpleNamespace(hook_registry=hook_registry)
    effect = CommandEffect()
    return SimpleNamespace(config=config, agent=agent, effect=effect)


def test_set_approval_rule_is_session_scoped() -> None:
    ctx = _build_ctx()

    result = _handle_set_approval_rule(
        SetApprovalRuleCommand(target="tool:shell", action="deny"),
        ctx,
    )

    assert ctx.config.approval.rules == []
    session_rules = getattr(ctx.agent, "session_approval_rules")
    assert len(session_rules) == 1
    assert session_rules[0].tool_name == "shell"
    assert session_rules[0].action == "deny"
    rules = result.state["rules"]
    assert isinstance(rules, list)
    assert rules[0]["tool_name"] == "shell"
    assert any(
        event.level == "success" and event.message == "Updated session approval rule"
        for event in ctx.effect.notifications
    )


def test_set_global_approval_rule_updates_config_and_runtime(monkeypatch) -> None:
    ctx = _build_ctx()
    saved = {}

    def fake_save(self, approval):
        saved["default_mode"] = approval.default_mode
        saved["rules"] = [(rule.tool_name, rule.action) for rule in approval.rules]
        return "/tmp/config.yaml"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.command.builtin.approval.WorkspaceConfigStore.save_approval_config",
        fake_save,
    )

    result = _handle_set_global_approval_rule(
        SetGlobalApprovalRuleCommand(target="tool:shell", action="warn"),
        ctx,
    )

    assert saved["rules"] == [("shell", "warn")]
    assert len(ctx.config.approval.rules) == 1
    assert ctx.config.approval.rules[0].tool_name == "shell"
    assert ctx.config.approval.rules[0].action == "warn"
    assert getattr(ctx.agent, "session_approval_rules", []) == []
    assert result.state["saved_path"] == "/tmp/config.yaml"
    assert any(
        event.level == "success"
        and "Updated workspace approval rule" in event.message
        for event in ctx.effect.notifications
    )


def test_set_global_approval_rule_replaces_same_target(tmp_path, monkeypatch) -> None:
    workspace_config = tmp_path / ".rcoder" / "config.yaml"
    monkeypatch.setattr(
        ConfigLoader,
        "WORKSPACE_CONFIG_PATH",
        workspace_config,
    )
    ctx = _build_ctx()
    ctx.config.approval.rules = [ApprovalRuleConfig(tool_name="shell", action="deny")]

    _handle_set_global_approval_rule(
        SetGlobalApprovalRuleCommand(target="tool:shell", action="allow"),
        ctx,
    )

    assert [(rule.tool_name, rule.action) for rule in ctx.config.approval.rules] == [
        ("shell", "allow")
    ]
    assert load_yaml_config(workspace_config)["approval"]["rules"] == [
        {"tool_name": "shell", "action": "allow"}
    ]


def test_unset_approval_rule_removes_session_rule() -> None:
    ctx = _build_ctx()
    _handle_set_approval_rule(
        SetApprovalRuleCommand(target="tool:shell", action="deny"), ctx
    )
    assert len(getattr(ctx.agent, "session_approval_rules")) == 1

    result = _handle_unset_approval_rule(
        UnsetApprovalRuleCommand(target="tool:shell"), ctx
    )

    assert getattr(ctx.agent, "session_approval_rules") == []
    assert any(
        event.level == "success" and "Removed session approval rule" in event.message
        for event in ctx.effect.notifications
    )
    assert result.state is not None


def test_unset_approval_rule_errors_when_rule_missing() -> None:
    ctx = _build_ctx()

    _handle_unset_approval_rule(UnsetApprovalRuleCommand(target="tool:shell"), ctx)

    assert any(
        event.level == "error" and "No session approval rule" in event.message
        for event in ctx.effect.notifications
    )


def test_unset_global_approval_rule_removes_and_saves(monkeypatch) -> None:
    ctx = _build_ctx()
    saved = {}

    def fake_save(self, approval):
        saved["rules"] = [(rule.tool_name, rule.action) for rule in approval.rules]
        return "/tmp/config.yaml"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.command.builtin.approval.WorkspaceConfigStore.save_approval_config",
        fake_save,
    )
    _handle_set_global_approval_rule(
        SetGlobalApprovalRuleCommand(target="tool:shell", action="warn"), ctx
    )
    assert saved["rules"] == [("shell", "warn")]

    _handle_unset_global_approval_rule(
        UnsetGlobalApprovalRuleCommand(target="tool:shell"), ctx
    )

    assert ctx.config.approval.rules == []
    assert saved["rules"] == []
    assert any(
        event.level == "success"
        and "Removed workspace approval rule" in event.message
        for event in ctx.effect.notifications
    )


def test_pattern_command_parser_preserves_quoted_shell_signature() -> None:
    signature = '{"command":"echo hello","cwd":"C:/work tree"}'

    parsed = _parse_set_approval(
        f"/approval set source=builtin,tool=shell '{signature}' allow",
        None,
    )
    unset = _parse_unset_approval(
        f"/approval unset source=builtin,tool=shell '{signature}'",
        None,
    )
    workspace = _parse_set_global_approval(
        "/approval set-workspace source=builtin,tool=edit_file src/** deny",
        None,
    )

    assert parsed == SetApprovalRuleCommand(
        target="source=builtin,tool=shell",
        action="allow",
        pattern=signature,
    )
    assert unset == UnsetApprovalRuleCommand(
        target="source=builtin,tool=shell",
        pattern=signature,
    )
    assert workspace == SetGlobalApprovalRuleCommand(
        target="source=builtin,tool=edit_file",
        action="deny",
        pattern="src/**",
    )


def test_session_rule_edit_keeps_runtime_scope_binding() -> None:
    ctx = _build_ctx()
    persisted = []
    ctx.agent.persist_runtime_snapshot = lambda: persisted.append(True)
    ctx.agent.session_approval_rules = [
        ApprovalRuleConfig(
            tool_name="edit_file",
            tool_source="builtin",
            pattern="src/**",
            scope_key='{"session_id":"session-1"}',
            action="deny",
        )
    ]

    _handle_set_approval_rule(
        SetApprovalRuleCommand(
            target="source=builtin,tool=edit_file",
            pattern="src/**",
            action="allow",
        ),
        ctx,
    )

    [updated] = ctx.agent.session_approval_rules
    assert updated.action == "allow"
    assert updated.scope_key == '{"session_id":"session-1"}'
    assert persisted == [True]

    _handle_unset_approval_rule(
        UnsetApprovalRuleCommand(
            target="source=builtin,tool=edit_file",
            pattern="src/**",
        ),
        ctx,
    )
    assert ctx.agent.session_approval_rules == []
    assert persisted == [True, True]
