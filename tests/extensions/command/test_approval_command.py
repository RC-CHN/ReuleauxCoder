from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig, Config
from reuleauxcoder.domain.hooks import HookPoint
from reuleauxcoder.domain.hooks.builtin import ToolPolicyGuardHook
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.extensions.command.builtin.approval import (
    SetApprovalRuleCommand,
    SetGlobalApprovalRuleCommand,
    _handle_set_approval_rule,
    _handle_set_global_approval_rule,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel


def _build_ctx() -> SimpleNamespace:
    config = Config(api_key="key", approval=ApprovalConfig())
    hook_registry = HookRegistry()
    hook_registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        ToolPolicyGuardHook(approval_config=config.approval),
    )
    agent = SimpleNamespace(hook_registry=hook_registry)
    ui_bus = UIEventBus()
    return SimpleNamespace(config=config, agent=agent, ui_bus=ui_bus)


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
    assert result.payload["rules"][0]["tool_name"] == "shell"
    assert any(
        event.level == UIEventLevel.SUCCESS and event.message == "Updated session approval rule"
        for event in ctx.ui_bus._history
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
    assert result.payload["saved_path"] == "/tmp/config.yaml"
    assert any(
        event.level == UIEventLevel.SUCCESS and "Updated global approval rule" in event.message
        for event in ctx.ui_bus._history
    )


def test_set_global_approval_rule_replaces_same_target() -> None:
    ctx = _build_ctx()
    ctx.config.approval.rules = [ApprovalRuleConfig(tool_name="shell", action="deny")]

    _handle_set_global_approval_rule(
        SetGlobalApprovalRuleCommand(target="tool:shell", action="allow"),
        ctx,
    )

    assert [(rule.tool_name, rule.action) for rule in ctx.config.approval.rules] == [("shell", "allow")]
