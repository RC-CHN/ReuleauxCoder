from types import SimpleNamespace

from reuleauxcoder.app.commands.models import (
    CommandEffect,
    CommandEffectBuilder,
    OpenViewRequest,
)
from reuleauxcoder.app.commands.view_models import MarkdownViewModel
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
    builder = CommandEffectBuilder()
    ctx = SimpleNamespace(ui_bus=builder)

    def handler(command, command_ctx):
        command_ctx.ui_bus.success("done")
        return CommandEffect(action="continue")

    action = _action(handler)
    parsed = SimpleNamespace(action=action, command=object())
    result = ActionRegistry().dispatch(parsed, ctx)

    assert real_bus._history == []
    assert [notice.message for notice in result.notifications] == ["done"]


def test_builder_deduplicates_legacy_returned_view_request() -> None:
    builder = CommandEffectBuilder()
    builder.open_view("sessions", title="Sessions", reuse_key="sessions")

    result = builder.build(
        CommandEffect(
            view_requests=[
                OpenViewRequest(
                    view_type="sessions",
                    title="Sessions",
                    view_model=builder.view_requests[0].view_model,
                    reuse_key="sessions",
                )
            ]
        )
    )

    assert len(result.view_requests) == 1


def test_builder_creates_typed_view_models() -> None:
    builder = CommandEffectBuilder()
    builder.open_view("help", title="Help", payload={"markdown": "# Help"})

    (view,) = builder.view_requests

    assert isinstance(view.view_model, MarkdownViewModel)
    assert view.payload == {"markdown": "# Help"}


def test_cli_applies_command_effect_once() -> None:
    builder = CommandEffectBuilder()
    builder.info("hello")
    builder.open_view("help", title="Help", payload={"markdown": "# Help"})
    result = builder.build(CommandEffect())
    bus = UIEventBus()

    _apply_command_effect(result, bus)

    assert [event.message for event in bus._history] == ["hello", "Open view: Help"]
