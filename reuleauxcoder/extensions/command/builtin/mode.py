"""Builtin mode command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.view_models import ModeProfileViewModel, ModesViewModel
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class SwitchModeCommand:
    mode_name: str


def _parse_show_mode(user_input: str, parse_ctx):
    if matches_any(user_input, ("/mode", "/mode ls", "/mode list", "/mode show")):
        return EmptyCommand()
    return None


def _parse_current_mode(user_input: str, parse_ctx):
    if matches_any(user_input, ("/mode current", "/mode now")):
        return EmptyCommand()
    return None


def _parse_switch_mode(user_input: str, parse_ctx):
    captures = match_template(user_input, "/mode switch {mode+}")
    if captures is None:
        captures = match_template(user_input, "/mode {mode+}")
    if captures is None:
        return None

    try:
        mode = non_empty_text(reject=frozenset({"ls", "list", "show", "switch"})).parse(
            captures["mode"]
        )
    except ParamParseError:
        return None

    return SwitchModeCommand(mode_name=mode)


def _handle_show_mode(command, ctx) -> CommandEffect:
    view = _build_mode_profiles_view(
        ctx.config, getattr(ctx.agent, "active_mode", None)
    )

    ctx.effect.open_view(
        view.view_type,
        title="Modes",
        view_model=view,
        reuse_key="mode_profiles",
    )

    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_current_mode(command, ctx) -> CommandEffect:
    mode_name = getattr(ctx.agent, "active_mode", None) or getattr(
        ctx.config, "active_mode", None
    )
    if mode_name:
        mode = (getattr(ctx.config, "modes", {}) or {}).get(mode_name)
        description = getattr(mode, "description", "") if mode is not None else ""
        suffix = f" - {description}" if description else ""
        ctx.effect.info(
            f"Current mode: {mode_name}{suffix}",
            kind=UIEventKind.COMMAND,
            mode_name=mode_name,
        )
        return ctx.effect.finish(
            control="continue", state_changes={"active_mode": mode_name}
        )

    ctx.effect.warning("No active mode set.", kind=UIEventKind.COMMAND)
    return ctx.effect.finish(control="continue", state_changes={"active_mode": None})


def _handle_switch_mode(command, ctx) -> CommandEffect:
    mode_name = command.mode_name
    modes = getattr(ctx.config, "modes", {}) or {}

    if mode_name not in modes:
        ctx.effect.error(
            f"Unknown mode '{mode_name}'. Use /mode to list available modes.",
            kind=UIEventKind.COMMAND,
            mode_name=mode_name,
        )
        return ctx.effect.finish(control="continue")

    ctx.agent.set_mode(mode_name)

    ctx.effect.success(
        f"Switched session mode to '{mode_name}'",
        kind=UIEventKind.COMMAND,
        mode_name=mode_name,
    )

    view = _build_mode_profiles_view(
        ctx.config, getattr(ctx.agent, "active_mode", None)
    )
    ctx.effect.refresh_view(
        view.view_type,
        title="Modes",
        view_model=view,
        reuse_key="mode_profiles",
    )

    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _build_mode_profiles_view(config, active_mode: str | None) -> ModesViewModel:
    modes = getattr(config, "modes", {}) or {}
    current = active_mode or getattr(config, "active_mode", None)

    mode_items = tuple(
        ModeProfileViewModel(
            name=name,
            active=current == name,
            description=getattr(mode, "description", "") or "",
            tools=tuple(getattr(mode, "tools", []) or []),
            prompt_append=getattr(mode, "prompt_append", "") or "",
            allowed_subagent_modes=tuple(
                getattr(mode, "allowed_subagent_modes", []) or []
            ),
        )
        for name, mode in sorted(modes.items())
    )
    diagnostics = (
        ("No modes configured. Add modes.profiles in config.yaml.",)
        if not modes
        else ()
    )
    return ModesViewModel(
        active_mode=current,
        modes=mode_items,
        diagnostics=diagnostics,
    )


def _build_mode_profiles_payload(config, active_mode: str | None) -> dict:
    return _build_mode_profiles_view(config, active_mode).to_payload()


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="mode.show",
                feature_id="mode",
                description="Show available modes and the current session mode",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mode"),),
                parser=_parse_show_mode,
                handler=_handle_show_mode,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="mode.current",
                feature_id="mode",
                description="[session] Show the current session mode",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/mode current"), slash_trigger("/mode now")),
                parser=_parse_current_mode,
                handler=_handle_current_mode,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="mode.switch",
                feature_id="mode",
                description="[session] Switch the active session mode",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/mode switch <name>"),
                    slash_trigger("/mode <name>"),
                ),
                parser=_parse_switch_mode,
                handler=_handle_switch_mode,
            ),
        ]
    )
