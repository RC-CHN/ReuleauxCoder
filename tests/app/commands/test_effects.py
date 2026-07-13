from types import SimpleNamespace

from reuleauxcoder.app.commands.models import (
    CommandEffect,
)
from reuleauxcoder.app.commands.view_models import HelpViewModel
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.interfaces.cli.commands import _apply_command_effect
from reuleauxcoder.interfaces.events import UIEventBus


def _action(handler) -> ActionSpec:
    return ActionSpec(
        action_id="test",
        feature_id="test",
        description="test",
        ui_targets=frozenset({"cli"}),
        handler=handler,
    )


def test_dispatch_records_notifications_without_publishing() -> None:
    real_bus = UIEventBus()
    effect = CommandEffect()
    ctx = SimpleNamespace(effect=effect)

    def handler(command, command_ctx):
        command_ctx.effect.success("done")
        return command_ctx.effect.finish(control="continue")

    action = _action(handler)
    parsed = SimpleNamespace(action=action, command=object())
    result = ActionRegistry().dispatch(parsed, ctx)

    assert real_bus._history == []
    assert [notice.message for notice in result.notifications] == ["done"]


def test_effect_requires_and_preserves_typed_view_model() -> None:
    effect = CommandEffect()
    model = HelpViewModel(sections=())
    effect.open_view("help", title="Help", view_model=model)

    (view,) = effect.views

    assert view.view_model is model


def test_cli_applies_command_effect_once() -> None:
    result = CommandEffect()
    result.info("hello")
    result.open_view("help", title="Help", view_model=HelpViewModel(sections=()))
    result.finish()
    bus = UIEventBus()

    _apply_command_effect(result, bus)

    assert [event.message for event in bus._history] == ["hello", "Open view: Help"]
