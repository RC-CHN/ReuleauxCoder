"""Builtin thinking command — inspect reasoning content and control display."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.params import EnumParam, ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.commands.view_models import (
    ThinkingEffortLevelViewModel,
    ThinkingEffortViewModel,
)
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
    if matches_any(user_input, ("/thinking effort",), case_insensitive=True):
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


def _handle_show(_command, ctx) -> CommandEffect:
    content = getattr(ctx.agent, "last_reasoning_content", None)
    if not content:
        ctx.effect.info(
            "No reasoning content in last turn.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    ctx.effect.reasoning(
        content,
        kind=UIEventKind.COMMAND,
        title="Reasoning",
    )
    return ctx.effect.finish(control="continue")


def _handle_inline(_command, ctx) -> CommandEffect:
    current = getattr(ctx.agent, "reasoning_display_mode", "quiet")
    new_mode = "inline" if current == "quiet" else "quiet"
    ctx.agent.reasoning_display_mode = new_mode
    ctx.effect.info(
        f"Reasoning display: {new_mode}.",
        kind=UIEventKind.COMMAND,
    )
    return ctx.effect.finish(control="continue")


def _handle_effort_show(_command, ctx) -> CommandEffect:
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
                profile_default = (
                    getattr(profile, "reasoning_effort", None) or "(not set)"
                )

    # Build available values display
    mapping = (
        getattr(llm, "reasoning_effort_values", None) or DEFAULT_REASONING_EFFORT_VALUES
    )
    param = getattr(llm, "reasoning_effort_param", "reasoning_effort")

    view = ThinkingEffortViewModel(
        current=current,
        param=param,
        profile_default=profile_default,
        levels=tuple(
            ThinkingEffortLevelViewModel(
                label=label, api_value=str(mapping.get(label, label))
            )
            for label in ("low", "medium", "high")
        ),
    )
    ctx.effect.open_view(
        view.view_type,
        title="Reasoning Effort",
        view_model=view,
        reuse_key="thinking_effort",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_effort_set(command, ctx) -> CommandEffect:
    level = command.level
    llm = ctx.agent.llm
    old = getattr(llm, "reasoning_effort", None) or "(not set)"

    # Validate against available values
    mapping = (
        getattr(llm, "reasoning_effort_values", None) or DEFAULT_REASONING_EFFORT_VALUES
    )
    if level not in mapping:
        available = ", ".join(sorted(mapping.keys()))
        ctx.effect.error(
            f"'{level}' is not available. Available values: {available}.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    api_val = mapping[level]
    param = getattr(llm, "reasoning_effort_param", "reasoning_effort")

    # Apply to LLM client (session only, no config write)
    llm.reasoning_effort = level

    ctx.effect.success(
        f"Reasoning effort set to: [bold]{level}[/bold] "
        f"(API: [dim]{api_val}[/dim] via [dim]{param}[/dim], was: {old}).",
        kind=UIEventKind.COMMAND,
    )
    return ctx.effect.finish(control="continue")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


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
                during_turn=DuringTurnPolicy.IMMEDIATE,
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
                during_turn=DuringTurnPolicy.IMMEDIATE,
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
                during_turn=DuringTurnPolicy.IMMEDIATE,
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
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
        ]
    )
