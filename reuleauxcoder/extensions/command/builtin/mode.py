"""Builtin mode command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
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
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.interfaces.cli.views.common import render_markdown_panel
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.interfaces.view_registration import register_view


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


@register_view(view_type="mode_profiles", ui_targets={"cli"})
def render_mode_profiles_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    markdown = payload.get("markdown")
    return isinstance(markdown, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown,
        title="Modes",
    )


def _handle_show_mode(command, ctx) -> CommandResult:
    payload = _build_mode_profiles_payload(
        ctx.config, getattr(ctx.agent, "active_mode", None)
    )

    ctx.ui_bus.open_view(
        "mode_profiles",
        title="Modes",
        payload=payload,
        reuse_key="mode_profiles",
    )

    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="mode_profiles",
                title="Modes",
                payload=payload,
                reuse_key="mode_profiles",
            )
        ],
        payload=payload,
    )


def _handle_current_mode(command, ctx) -> CommandResult:
    mode_name = getattr(ctx.agent, "active_mode", None) or getattr(
        ctx.config, "active_mode", None
    )
    if mode_name:
        mode = (getattr(ctx.config, "modes", {}) or {}).get(mode_name)
        description = getattr(mode, "description", "") if mode is not None else ""
        suffix = f" - {description}" if description else ""
        ctx.ui_bus.info(
            f"Current mode: {mode_name}{suffix}",
            kind=UIEventKind.COMMAND,
            mode_name=mode_name,
        )
        return CommandResult(action="continue", payload={"active_mode": mode_name})

    ctx.ui_bus.warning("No active mode set.", kind=UIEventKind.COMMAND)
    return CommandResult(action="continue", payload={"active_mode": None})


def _handle_switch_mode(command, ctx) -> CommandResult:
    mode_name = command.mode_name
    modes = getattr(ctx.config, "modes", {}) or {}

    if mode_name not in modes:
        ctx.ui_bus.error(
            f"Unknown mode '{mode_name}'. Use /mode to list available modes.",
            kind=UIEventKind.COMMAND,
            mode_name=mode_name,
        )
        return CommandResult(action="continue")

    ctx.agent.set_mode(mode_name)

    ctx.ui_bus.success(
        f"Switched session mode to '{mode_name}'",
        kind=UIEventKind.COMMAND,
        mode_name=mode_name,
    )

    payload = _build_mode_profiles_payload(
        ctx.config, getattr(ctx.agent, "active_mode", None)
    )
    ctx.ui_bus.refresh_view(
        "mode_profiles",
        title="Modes",
        payload=payload,
        reuse_key="mode_profiles",
    )

    return CommandResult(action="continue", payload=payload)


def _build_mode_profiles_payload(config, active_mode: str | None) -> dict:
    modes = getattr(config, "modes", {}) or {}
    current = active_mode or getattr(config, "active_mode", None)

    lines: list[str] = []
    mode_items: list[dict] = []

    if current:
        lines.append(f"**Current active mode:** `{current}`")
    else:
        lines.append("**Current active mode:** `(none)`")

    lines.append("")

    if not modes:
        lines.append("> No modes configured. Add `modes.profiles` in config.yaml.")
    else:
        lines.append("**Modes:**")
        lines.append("")
        for name in sorted(modes):
            m = modes[name]
            is_active = current == name
            marker = " ✓" if is_active else ""

            tools = list(getattr(m, "tools", []) or [])
            allowed_subagent_modes = list(
                getattr(m, "allowed_subagent_modes", []) or []
            )
            prompt_append = getattr(m, "prompt_append", "") or ""

            mode_items.append(
                {
                    "name": name,
                    "active": is_active,
                    "description": getattr(m, "description", "") or "",
                    "tools": tools,
                    "prompt_append": prompt_append,
                    "allowed_subagent_modes": allowed_subagent_modes,
                }
            )

            lines.append(f"- **{name}**{marker}")
            if getattr(m, "description", ""):
                lines.append(f"  - description: {m.description}")
            lines.append(
                "  - tools: "
                + (
                    "`*`"
                    if "*" in tools
                    else ", ".join(f"`{t}`" for t in tools)
                    if tools
                    else "(none)"
                )
            )
            lines.append(
                "  - allowed_subagent_modes: "
                + (
                    ", ".join(f"`{n}`" for n in allowed_subagent_modes)
                    if allowed_subagent_modes
                    else "(none)"
                )
            )
            if prompt_append:
                lines.append(f"  - prompt_append: `{prompt_append}`")
            lines.append("")

    return {
        "active_mode": current,
        "markdown": "\n".join(lines),
        "modes": mode_items,
    }


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
