from types import SimpleNamespace

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.interaction_contracts import ChooseOneResponse, ConfirmResponse
from reuleauxcoder.app.rpc.codec import decode, encode
from reuleauxcoder.extensions.command.builtin.compact import (
    CompactContextCommand,
    _handle_compact,
    command_panel_spec,
    register_actions,
)


def context(*, menus=True, selection="summarize", confirmed=False):
    calls = []
    prompts = []
    agent = SimpleNamespace(messages=[{"tokens": 1200}], llm=None)
    agent.context = SimpleNamespace(
        predict_request_tokens=lambda messages: messages[0]["tokens"],
        request_input_limit=8000,
    )

    def compress(strategy, llm):
        calls.append(strategy)
        agent.messages[0]["tokens"] = 400
        return True

    def choose(request):
        prompts.append(request)
        return ChooseOneResponse(selection, cancelled=selection is None)

    agent.force_compress_context = compress
    ctx = SimpleNamespace(
        agent=agent,
        effect=CommandEffect(),
        ui_profile=UIProfile(
            "tui" if menus else "cli",
            "Test",
            frozenset({UICapability.MENUS, UICapability.TEXT_INPUT})
            if menus
            else frozenset({UICapability.TEXT_INPUT}),
        ),
        ui_interactor=SimpleNamespace(
            choose_one=choose, confirm=lambda request: ConfirmResponse(confirmed)
        ),
    )
    return ctx, calls, prompts


def test_compact_opens_typed_picker_without_mutating_context():
    ctx, calls, _ = context()
    effect = _handle_compact(CompactContextCommand(), ctx)
    model = decode(encode(effect.views[0].view_model))
    panel = command_panel_spec().build_for(model, "Compact")
    assert "1,200 / 8,000" in panel.title
    assert [item.action.command.force_strategy for item in panel.items] == [
        "summarize",
        "snip",
        "collapse",
    ]
    assert not panel.show_auxiliary_actions
    assert calls == []
    _handle_compact(panel.items[0].action.command, ctx)
    assert calls == ["summarize"]
    assert "1,200 → 400" in ctx.effect.notifications[-1].message


def test_cli_uses_the_same_choices_and_cancel_does_not_compact():
    ctx, calls, prompts = context(menus=False, selection=None)
    _handle_compact(CompactContextCommand(), ctx)
    assert [item.id for item in prompts[0].items] == ["summarize", "snip", "collapse"]
    assert prompts[0].initial_id == "summarize"
    assert calls == []


def test_deep_compaction_requires_confirmation_even_for_explicit_command():
    ctx, calls, _ = context()
    _handle_compact(CompactContextCommand("collapse"), ctx)
    assert calls == []
    ctx.ui_interactor.confirm = lambda request: ConfirmResponse(True)
    _handle_compact(CompactContextCommand("collapse"), ctx)
    assert calls == ["collapse"]


def test_catalog_opens_picker_by_default_and_keeps_explicit_strategy_syntax():
    registry = ActionRegistry()
    register_actions(registry)
    ctx, _, _ = context()
    parsed = registry.parse("/compact", ui_profile=ctx.ui_profile)
    assert parsed.action.preview
    assert parsed.command == CompactContextCommand()
    parsed = registry.parse("/compact force snip", ui_profile=ctx.ui_profile)
    assert parsed.command == CompactContextCommand("snip")
