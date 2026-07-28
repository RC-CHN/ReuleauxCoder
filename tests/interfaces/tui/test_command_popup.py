from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.interfaces.tui.command_popup import (
    build_popup_entries,
    filter_entries,
)
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


_PROFILE = UIProfile(
    ui_id="cli",
    display_name="CLI",
    capabilities=frozenset({UICapability.TEXT_INPUT}),
)


def _action(
    action_id: str,
    description: str,
    *triggers: str,
    interactive: bool = False,
) -> ActionSpec:
    return ActionSpec(
        action_id=action_id,
        feature_id=action_id.split(".", 1)[0],
        description=description,
        ui_targets=frozenset({"cli"}),
        triggers=tuple(slash_trigger(trigger) for trigger in triggers),
        interactive=interactive,
    )


def _registry() -> ActionRegistry:
    return ActionRegistry(
        [
            _action("mode.show", "Choose the active session mode", "/mode"),
            _action(
                "mode.switch", "Switch mode", "/mode switch <name>", "/mode <name>"
            ),
            _action(
                "sessions.list",
                "Browse and resume saved sessions",
                "/session",
                "/session all",
                "/sessions",
                "/sessions all",
            ),
            _action("agents.list", "Inspect background agents", "/agents", "/jobs"),
            _action(
                "agents.cancel", "Cancel a job", "/agents cancel <id>", "/jobs cancel"
            ),
            _action(
                "thinking.effort",
                "Set reasoning effort",
                "/thinking effort {level}",
                interactive=True,
            ),
            _action("help.show", "Show command help", "/help"),
        ]
    )


def test_build_entries_dedupes_aliases_to_shortest_completion() -> None:
    entries = build_popup_entries(_registry(), _PROFILE)
    completions = [entry.completion for entry in entries]

    assert "/session" in completions
    assert "/sessions" not in completions
    assert "/agents" in completions
    assert "/jobs" not in completions


def test_build_entries_adds_synthetic_root_for_families_without_bare() -> None:
    entries = build_popup_entries(_registry(), _PROFILE)
    thinking = [entry for entry in entries if entry.completion == "/thinking"]

    assert thinking, "expected a synthetic /thinking root entry"
    assert thinking[0].interactive is True


def test_build_entries_strips_placeholders() -> None:
    entries = build_popup_entries(_registry(), _PROFILE)
    by_completion = {entry.completion: entry for entry in entries}

    assert by_completion["/mode switch"].has_arg is True
    assert by_completion["/agents cancel"].has_arg is True
    assert by_completion["/help"].has_arg is False


def test_filter_roots_by_prefix() -> None:
    entries = build_popup_entries(_registry(), _PROFILE)

    roots = [entry.completion for entry in filter_entries(entries, "/mo")]
    assert roots == ["/mode"]


def test_filter_lists_subcommands_after_space() -> None:
    entries = build_popup_entries(_registry(), _PROFILE)

    subs = [entry.completion for entry in filter_entries(entries, "/agents ")]
    assert subs == ["/agents cancel"]


def test_filter_empty_for_non_slash_input() -> None:
    entries = build_popup_entries(_registry(), _PROFILE)

    assert filter_entries(entries, "hello") == ()
