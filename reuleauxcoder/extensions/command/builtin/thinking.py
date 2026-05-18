"""Builtin thinking command — inspect reasoning content and control display."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import EnumParam, ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.domain.config.models import DEFAULT_REASONING_EFFORT_VALUES
from reuleauxcoder.interfaces.events import UIEventKind

_VALID_EFFORTS = frozenset({"low", "medium", "high"})
_VALID_DISPLAY_MODES = frozenset({"quiet", "inline"})


@dataclass(frozen=True, slots=True)
class SetEffortCommand:
    level: str


@dataclass(frozen=True, slots=True)
class ToggleInlineCommand:
    pass


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_show(user_input: str, _parse_ctx):
    if match_template(user_input, "/thinking") is not None:
        return EmptyCommand()
    return None


def _parse_inline(user_input: str, _parse_ctx):
    if matches_any(user_input, ("/thinking inline",), case_insensitive=True):
        return ToggleInlineCommand()
    return None


def _parse_effort_show(user_input: str, _parse_ctx):
    if matches_any(
        user_input, ("/thinking effort",), case_insensitive=True
    ):
        return EmptyCommand()
    return None


def _parse_effort_set(user_input: str, _parse_ctx):
    captures = match_template(
        user_input, "/thinking effort {level}", case_insensitive=True
    )
    if captures is None:
        return None

    try:
        level = EnumParam(values=_VALID_EFFORTS, case_insensitive=True).parse(
            captures["level"]
        )
    except ParamParseError:
        return None

    return SetEffortCommand(level=level)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_show(_command, ctx) -> CommandResult:
    content = getattr(ctx.agent, "last_reasoning_content", None)
    if not content:
        ctx.ui_bus.info(
            "No reasoning content in last turn.",
            kind=UIEventKind.COMMAND,
        )
        return CommandResult(action="continue")

    ctx.ui_bus.info(
        content,
        kind=UIEventKind.COMMAND,
        title="Reasoning",
        is_reasoning=True,
    )
    return CommandResult(action="continue")


def _handle_inline(_command, ctx) -> CommandResult:
    current = getattr(ctx.agent, "reasoning_display_mode", "quiet")
    new_mode = "inline" if current == "quiet" else "quiet"
    ctx.agent.reasoning_display_mode = new_mode
    ctx.ui_bus.info(
        f"Reasoning display: {new_mode}.",
        kind=UIEventKind.COMMAND,
    )
    return CommandResult(action="continue")


def _handle_effort_show(_command, ctx) -> CommandResult:
    llm = ctx.agent.llm
    current = getattr(llm, "reasoning_effort", None) or "(not set)"

    # Resolve profile default
    profile_default = "(not set)"
    config = ctx.config
    if config is not None:
        active_main = getattr(config, "active_main_model_profile", None)
        if active_main is not None:
            profiles = getattr(config, "model_profiles", {}) or {}
            profile = profiles.get(active_main)
            if profile is not None:
                profile_default = getattr(profile, "reasoning_effort", None) or "(not set)"

    # Build available values display
    mapping = getattr(llm, "reasoning_effort_values", None) or DEFAULT_REASONING_EFFORT_VALUES
    param = getattr(llm, "reasoning_effort_param", "reasoning_effort")

    value_lines: list[str] = []
    for label in ("low", "medium", "high"):
        api_val = mapping.get(label, label)
        marker = " ✓" if label == current else ""
        value_lines.append(f"  {label} → {api_val}{marker}")

    lines = [
        f"Reasoning effort: [bold]{current}[/bold]",
        f"Parameter: [dim]{param}[/dim]",
        "",
        "Available:",
        *value_lines,
        "",
        f"(profile default: {profile_default})",
    ]

    ctx.ui_bus.info(
        "\n".join(lines),
        kind=UIEventKind.COMMAND,
    )
    return CommandResult(action="continue")


def _handle_effort_set(command, ctx) -> CommandResult:
    level = command.level
    llm = ctx.agent.llm
    old = getattr(llm, "reasoning_effort", None) or "(not set)"

    # Validate against available values
    mapping = getattr(llm, "reasoning_effort_values", None) or DEFAULT_REASONING_EFFORT_VALUES
    if level not in mapping:
        available = ", ".join(sorted(mapping.keys()))
        ctx.ui_bus.error(
            f"'{level}' is not available. Available values: {available}.",
            kind=UIEventKind.COMMAND,
        )
        return CommandResult(action="continue")

    api_val = mapping[level]
    param = getattr(llm, "reasoning_effort_param", "reasoning_effort")

    # Apply to LLM client (session only, no config write)
    llm.reasoning_effort = level

    ctx.ui_bus.success(
        f"Reasoning effort set to: [bold]{level}[/bold] "
        f"(API: [dim]{api_val}[/dim] via [dim]{param}[/dim], was: {old}).",
        kind=UIEventKind.COMMAND,
    )
    return CommandResult(action="continue")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="thinking.show",
                feature_id="thinking",
                description="[session] Show reasoning content from the last turn",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/thinking"),),
                parser=_parse_show,
                handler=_handle_show,
            ),
            ActionSpec(
                action_id="thinking.toggle_inline",
                feature_id="thinking",
                description="[session] Toggle inline streaming of reasoning content",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/thinking inline"),),
                parser=_parse_inline,
                handler=_handle_inline,
            ),
            ActionSpec(
                action_id="thinking.show_effort",
                feature_id="thinking",
                description="Show current reasoning effort budget",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/thinking effort"),),
                parser=_parse_effort_show,
                handler=_handle_effort_show,
            ),
            ActionSpec(
                action_id="thinking.set_effort",
                feature_id="thinking",
                description="[session] Set reasoning effort (low/medium/high)",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/thinking effort {level}"),),
                parser=_parse_effort_set,
                handler=_handle_effort_set,
            ),
        ]
    )
