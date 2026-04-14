from types import SimpleNamespace

from reuleauxcoder.app.commands.dispatcher import dispatch_command
from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import ActionSpec, TriggerKind, TriggerSpec
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


CLI_PROFILE = UIProfile(ui_id="cli", display_name="CLI", capabilities=frozenset({UICapability.TEXT_INPUT}))
TUI_PROFILE = UIProfile(ui_id="tui", display_name="TUI", capabilities=frozenset())


def _slash_action(*, action_id: str = "test", parser=None, handler=None, ui_targets=frozenset({"cli"})) -> ActionSpec:
    return ActionSpec(
        action_id=action_id,
        feature_id="feature.test",
        description="test",
        ui_targets=ui_targets,
        required_capabilities=frozenset({UICapability.TEXT_INPUT}),
        triggers=(TriggerSpec(kind=TriggerKind.SLASH, value="/test"),),
        parser=parser,
        handler=handler,
    )


def test_action_registry_iter_actions_filters_by_ui_profile() -> None:
    registry = ActionRegistry([
        _slash_action(action_id="cli-only"),
        _slash_action(action_id="tui-only", ui_targets=frozenset({"tui"})),
    ])

    actions = registry.iter_actions(CLI_PROFILE)

    assert [action.action_id for action in actions] == ["cli-only"]


def test_action_registry_parse_returns_first_matching_action() -> None:
    def parser(user_input, parse_ctx):
        return {"value": user_input} if user_input == "/test" else None

    registry = ActionRegistry([_slash_action(parser=parser)])

    parsed = registry.parse("/test", ui_profile=CLI_PROFILE, current_session_id="s1")

    assert parsed is not None
    assert parsed.command == {"value": "/test"}
    assert parsed.action.action_id == "test"
    assert parsed.registry is registry


def test_action_registry_parse_skips_actions_without_available_slash_trigger() -> None:
    action = ActionSpec(
        action_id="palette-only",
        feature_id="feature.palette",
        description="palette",
        ui_targets=frozenset({"cli"}),
        triggers=(TriggerSpec(kind=TriggerKind.PALETTE, value="palette"),),
        parser=lambda user_input, parse_ctx: {"value": user_input},
    )
    registry = ActionRegistry([action])

    assert registry.parse("/test", ui_profile=CLI_PROFILE) is None


def test_action_registry_dispatch_returns_continue_when_handler_missing() -> None:
    registry = ActionRegistry()
    parsed = SimpleNamespace(action=_slash_action(handler=None), command={"x": 1}, registry=registry)

    result = registry.dispatch(parsed, ctx=SimpleNamespace())

    assert result == CommandResult(action="continue")


def test_dispatch_command_delegates_to_registry_dispatch() -> None:
    called = []

    def handler(command, ctx):
        called.append((command, ctx))
        return CommandResult(action="exit")

    registry = ActionRegistry([_slash_action(handler=handler)])
    parsed = SimpleNamespace(action=_slash_action(handler=handler), command={"ok": True}, registry=registry)
    ctx = SimpleNamespace()

    result = dispatch_command(parsed, ctx)

    assert result.action == "exit"
    assert called == [({"ok": True}, ctx)]
