from dataclasses import fields, replace
import re
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.commands.shared import EmptyCommand
from reuleauxcoder.app.commands.specs import ActionDescription, DuringTurnPolicy
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.extensions.command.builtin.processes import SecureProcessInputCommand
from reuleauxcoder.extensions.command.builtin.thinking import SetEffortCommand


PROFILE = UIProfile("tui", "TUI", frozenset(UICapability))


def _example(trigger):
    values = {
        "target": "tool=write_file",
        "action": "deny",
        "server": "github",
        "name": "coder",
        "profile": "fast",
        "id": "job-1",
        "id|all": "all",
        "#|id|latest": "latest",
        "text": "follow this up",
        "on|off": "on",
        "tokens|none": "1000",
        "snip|summarize|collapse": "snip",
    }
    text = re.sub(r"<([^>]+)>", lambda match: values[match[1]], trigger.value)
    return (
        text.replace("[pattern] ", "")
        .replace(" [pattern]", "")
        .replace("{level}", "high")
    )


@pytest.mark.parametrize("ui_id", ["cli", "tui", "vscode"])
def test_every_builtin_trigger_and_structured_request_share_dispatch(ui_id):
    profile = replace(PROFILE, ui_id=ui_id)
    builtin = create_builtin_action_registry()
    handled = []

    def handle(command, ctx):
        handled.append(command)
        ctx.effect.info("handled")
        return ctx.effect

    registry = ActionRegistry(
        [
            replace(action, handler=handle, audit=None)
            for action in builtin.iter_actions(profile)
        ]
    )
    agent = SimpleNamespace(current_session_id="session")
    bus = UIEventBus()
    service = CommandService(agent, SimpleNamespace(), bus, profile, registry)

    for action in builtin.iter_actions(profile):
        assert action.command_type is not object
        for trigger in action.matching_triggers(profile):
            text = _example(trigger)
            parsed = builtin.parse(text, ui_profile=profile)
            assert parsed is not None, text
            assert parsed.action.action_id == action.action_id, text
            assert isinstance(parsed.command, action.command_type), text
            text_result = service.submit(text)
            direct_result = service.submit(parsed.request)
            assert text_result == direct_result
            assert handled[-2:] == [parsed.command, parsed.command]
            assert [event.message for event in bus.history_snapshot()[-2:]] == [
                "handled",
                "handled",
            ]

    assert all(type(item) is ActionDescription for item in service.catalog.actions)
    assert not {"parser", "handler", "command_type", "audit"} & {
        field.name for field in fields(ActionDescription)
    }


def test_button_only_frontend_can_invoke_actions_without_slash_input():
    profile = UIProfile("tui", "Buttons", frozenset({UICapability.BUTTONS}))
    agent = SimpleNamespace(
        current_session_id=None, llm=SimpleNamespace(reasoning_effort="low")
    )
    service = CommandService(
        agent,
        SimpleNamespace(),
        UIEventBus(),
        profile,
        create_builtin_action_registry(),
    )

    result = service.submit(
        ActionRequest("thinking.set_effort", SetEffortCommand("high"))
    )

    assert result.control == "continue"
    assert agent.llm.reasoning_effort == "high"
    assert service.registry.parse("/thinking effort high", ui_profile=profile) is None


def test_structured_requests_enforce_capability_and_parameter_type():
    profile = UIProfile("cli", "Remote", frozenset({UICapability.TEXT_INPUT}))
    service = CommandService(
        SimpleNamespace(current_session_id=None),
        None,
        UIEventBus(),
        profile,
        create_builtin_action_registry(),
    )

    with pytest.raises(ValueError, match="Unavailable action: processes.secure_input"):
        service.submit(
            ActionRequest("processes.secure_input", SecureProcessInputCommand("pty-1"))
        )
    with pytest.raises(TypeError, match="Invalid parameters"):
        service.submit(ActionRequest("sessions.resume", EmptyCommand()))
    with pytest.raises(ValueError, match="Unavailable action"):
        service.submit(ActionRequest("unknown", EmptyCommand()))


def test_queue_uses_execution_time_session_and_clears_stop_at_dispatch():
    observed = []
    agent = SimpleNamespace(
        current_session_id="old", clear_stop_request=lambda: observed.append("clear")
    )
    builtin = create_builtin_action_registry()
    save = next(
        action
        for action in builtin.iter_actions(PROFILE)
        if action.action_id == "sessions.save"
    )

    def handle(command, ctx):
        observed.append(ctx.agent.current_session_id)
        return ctx.effect

    registry = ActionRegistry([replace(save, handler=handle, audit=None)])
    service = CommandService(agent, None, UIEventBus(), PROFILE, registry)

    assert service.submit("/save", during_turn=True).control == "queued"
    assert service.pending_commands == ("/save",)
    assert observed == []
    agent.current_session_id = "new"
    result = service.submit(service.next_pending())

    assert result.session_id == "new"
    assert observed == ["clear", "new"]
    assert service.next_pending() is None


def test_every_action_uses_declared_during_turn_policy():
    registry = create_builtin_action_registry()
    service = CommandService(
        SimpleNamespace(current_session_id=None), None, UIEventBus(), PROFILE, registry
    )
    for action in registry.iter_actions(PROFILE):
        text = _example(action.triggers[0])
        request = service.prepare_during_turn(text)
        if action.during_turn is DuringTurnPolicy.IMMEDIATE:
            assert request == registry.parse(text, ui_profile=PROFILE).request
            assert service.pending_commands == ()
        else:
            assert request is None
            assert service.pending_commands == (text,)
            assert service.next_pending().action_id == action.action_id


def test_duplicate_action_ids_cannot_shadow_structured_dispatch():
    action = create_builtin_action_registry().iter_actions(PROFILE)[0]
    with pytest.raises(ValueError, match="Duplicate command action"):
        ActionRegistry([action, action])
