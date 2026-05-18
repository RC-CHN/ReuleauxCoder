"""Tests for fuzzy command matching in CLI commands module."""

from __future__ import annotations

from reuleauxcoder.interfaces.cli.commands import (
    _extract_base_name,
    _levenshtein,
    _suggest_command,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import ActionSpec, TriggerKind, TriggerSpec
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLI_PROFILE = UIProfile(
    ui_id="cli",
    display_name="CLI",
    capabilities=frozenset({UICapability.TEXT_INPUT}),
)


def _make_registry(base_names: set[str]) -> ActionRegistry:
    registry = ActionRegistry()
    for name in sorted(base_names):
        registry.register(
            ActionSpec(
                action_id=f"{name}.test",
                feature_id=name,
                description=f"Test {name}",
                ui_targets=frozenset({"cli"}),
                triggers=(
                    TriggerSpec(
                        kind=TriggerKind.SLASH,
                        value=f"/{name}",
                        ui_targets=frozenset({"cli"}),
                        required_capabilities=frozenset({UICapability.TEXT_INPUT}),
                    ),
                ),
                parser=lambda u, c: object(),
                handler=lambda c, ctx: None,
            )
        )
    return registry


# ---------------------------------------------------------------------------
# _extract_base_name
# ---------------------------------------------------------------------------


class TestExtractBaseName:
    def test_simple_command(self):
        assert _extract_base_name("/thinking") == "thinking"

    def test_command_with_subcommand(self):
        assert _extract_base_name("/thinking inline") == "thinking"

    def test_command_with_template(self):
        assert _extract_base_name("/thinking effort {level}") == "thinking"

    def test_no_slash(self):
        assert _extract_base_name("hello") == ""

    def test_slash_only(self):
        assert _extract_base_name("/") == ""

    def test_empty(self):
        assert _extract_base_name("") == ""


# ---------------------------------------------------------------------------
# _levenshtein
# ---------------------------------------------------------------------------


class TestLevenshtein:
    def test_same(self):
        assert _levenshtein("hello", "hello") == 0

    def test_insert(self):
        assert _levenshtein("thiking", "thinking") == 1  # insert 'n'

    def test_delete(self):
        assert _levenshtein("thinking", "thiking") == 1  # delete 'n'

    def test_substitute(self):
        assert _levenshtein("thunking", "thinking") == 1  # u→i

    def test_two_edits(self):
        assert _levenshtein("thikn", "thinking") == 3  # insert n, i, g

    def test_empty_source(self):
        assert _levenshtein("", "hello") == 5

    def test_empty_target(self):
        assert _levenshtein("hello", "") == 5


# ---------------------------------------------------------------------------
# _suggest_command
# ---------------------------------------------------------------------------


class TestSuggestCommand:
    def test_exact_match_returns_none(self):
        """When the typed command exists, parser already handled it — but if
        parser returned None (e.g. subcommand error), don't suggest."""
        registry = _make_registry({"thinking", "help"})
        assert _suggest_command("/thinking", registry, _CLI_PROFILE) is None

    def test_one_edit_suggests(self):
        registry = _make_registry({"thinking", "help", "mode"})
        suggestion = _suggest_command("/thiking", registry, _CLI_PROFILE)
        assert suggestion is not None
        assert "thiking" in suggestion
        assert "thinking" in suggestion

    def test_two_edits_suggests(self):
        registry = _make_registry({"thinking", "help", "mode"})
        suggestion = _suggest_command("/thnkng", registry, _CLI_PROFILE)
        assert suggestion is not None
        assert "thinking" in suggestion

    def test_three_edits_no_suggestion(self):
        """Distance 3 should be too far."""
        registry = _make_registry({"thinking"})
        suggestion = _suggest_command("/thwabang", registry, _CLI_PROFILE)
        # Should return "Unknown command" but no suggestion
        assert suggestion is not None
        assert "Unknown command" in suggestion
        assert "Did you mean" not in suggestion

    def test_no_slash_input(self):
        registry = _make_registry({"thinking"})
        assert _suggest_command("hello world", registry, _CLI_PROFILE) is None

    def test_empty_slash(self):
        registry = _make_registry({"thinking"})
        assert _suggest_command("/", registry, _CLI_PROFILE) is None

    def test_short_typo_far(self):
        """Short words have tighter thresholds."""
        registry = _make_registry({"help", "mode"})
        # "hlp" → "help" is distance 1, should work
        suggestion = _suggest_command("/hlp", registry, _CLI_PROFILE)
        assert suggestion is not None
        assert "help" in suggestion

    def test_unknown_no_candidates(self):
        """Commands not close to anything."""
        registry = _make_registry({"help", "mode"})
        suggestion = _suggest_command("/xyzzy", registry, _CLI_PROFILE)
        assert suggestion is not None
        assert "Unknown command" in suggestion
        assert "Did you mean" not in suggestion

    def test_close_to_multiple_picks_closest(self):
        registry = _make_registry({"mode", "model"})
        # "modl" → "mode" = 1, "model" = 1, both equal distance
        # should pick alphabetically first ("mode")
        suggestion = _suggest_command("/modl", registry, _CLI_PROFILE)
        assert suggestion is not None
        assert "mode" in suggestion

    def test_subcommand_error_no_suggest(self):
        """Exact match of base command but parser rejected — don't suggest."""
        registry = _make_registry({"thinking"})
        # /thinking effort xml  → thinking is exact match, parser rejected
        # because the subcommand value is invalid
        assert _suggest_command("/thinking effort xml", registry, _CLI_PROFILE) is None
