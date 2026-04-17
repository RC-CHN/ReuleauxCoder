"""Builtin model command extension registration and handlers."""

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
from reuleauxcoder.app.runtime.session_state import build_session_runtime_state
from reuleauxcoder.infrastructure.persistence.workspace_config_store import WorkspaceConfigStore
from reuleauxcoder.interfaces.cli.views.common import render_markdown_panel
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.interfaces.view_registration import register_view


@dataclass(frozen=True, slots=True)
class SwitchModelCommand:
    profile_name: str


@dataclass(frozen=True, slots=True)
class UseMainModelCommand:
    profile_name: str


@dataclass(frozen=True, slots=True)
class UseSubModelCommand:
    profile_name: str


@dataclass(frozen=True, slots=True)
class SetMainModelCommand:
    profile_name: str


@dataclass(frozen=True, slots=True)
class SetSubModelCommand:
    profile_name: str


def _parse_show_model(user_input: str, parse_ctx):
    if matches_any(user_input, ("/model", "/model ls", "/model list", "/model show")):
        return EmptyCommand()
    return None


def _parse_use_main_model(user_input: str, parse_ctx):
    captures = match_template(user_input, "/model use-main {profile+}")
    if captures is None:
        return None

    try:
        profile = non_empty_text().parse(captures["profile"])
    except ParamParseError:
        return None

    return UseMainModelCommand(profile_name=profile)


def _parse_use_sub_model(user_input: str, parse_ctx):
    captures = match_template(user_input, "/model use-sub {profile+}")
    if captures is None:
        return None

    try:
        profile = non_empty_text().parse(captures["profile"])
    except ParamParseError:
        return None

    return UseSubModelCommand(profile_name=profile)


def _parse_set_main_model(user_input: str, parse_ctx):
    captures = match_template(user_input, "/model set-main {profile+}")
    if captures is None:
        return None

    try:
        profile = non_empty_text().parse(captures["profile"])
    except ParamParseError:
        return None

    return SetMainModelCommand(profile_name=profile)


def _parse_set_sub_model(user_input: str, parse_ctx):
    captures = match_template(user_input, "/model set-sub {profile+}")
    if captures is None:
        return None

    try:
        profile = non_empty_text().parse(captures["profile"])
    except ParamParseError:
        return None

    return SetSubModelCommand(profile_name=profile)


def _parse_switch_model(user_input: str, parse_ctx):
    captures = match_template(user_input, "/model {profile+}")
    if captures is None:
        return None

    try:
        profile = non_empty_text(
            reject=frozenset({"ls", "list", "show", "use-main", "use-sub", "set-main", "set-sub"})
        ).parse(captures["profile"])
    except ParamParseError:
        return None

    return SwitchModelCommand(profile_name=profile)


@register_view(view_type="model_profiles", ui_targets={"cli"})
def render_model_profiles_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    markdown = payload.get("markdown")
    return isinstance(markdown, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown,
        title="Model Profiles",
    )


def _handle_show_model(command, ctx) -> CommandResult:
    payload = _build_model_profiles_payload(
        ctx.config,
        runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
    )

    ctx.ui_bus.open_view(
        "model_profiles",
        title="Model Profiles",
        payload=payload,
        reuse_key="model_profiles",
    )

    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="model_profiles",
                title="Model Profiles",
                payload=payload,
                reuse_key="model_profiles",
            )
        ],
        payload=payload,
    )


def _resolve_profile(ctx, profile_name: str):
    profiles = getattr(ctx.config, "model_profiles", {}) or {}
    profile = profiles.get(profile_name)
    if profile is None:
        ctx.ui_bus.error(
            f"Unknown model profile '{profile_name}'. Use /model to list available profiles.",
            kind=UIEventKind.MODEL,
            profile_name=profile_name,
        )
    return profile


def _apply_main_profile_to_runtime(ctx, profile_name: str, profile) -> None:
    debug_trace = getattr(ctx.agent.llm, "debug_trace", getattr(ctx.config, "llm_debug_trace", False))
    ctx.agent.llm.reconfigure(
        model=profile.model,
        api_key=profile.api_key,
        base_url=profile.base_url,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        preserve_reasoning_content=profile.preserve_reasoning_content,
        backfill_reasoning_content_for_tool_calls=profile.backfill_reasoning_content_for_tool_calls,
        debug_trace=debug_trace,
    )
    ctx.agent.context.reconfigure(profile.max_context_tokens)
    setattr(ctx.agent, "active_main_model_profile", profile_name)


def _refresh_model_view(ctx) -> dict:
    payload = _build_model_profiles_payload(
        ctx.config,
        runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
    )
    ctx.ui_bus.refresh_view(
        "model_profiles",
        title="Model Profiles",
        payload=payload,
        reuse_key="model_profiles",
    )
    return payload


def _handle_switch_model(command, ctx) -> CommandResult:
    profile_name = command.profile_name
    profile = _resolve_profile(ctx, profile_name)
    if profile is None:
        return CommandResult(action="continue")

    _apply_main_profile_to_runtime(ctx, profile_name, profile)
    payload = _refresh_model_view(ctx)
    ctx.ui_bus.success(
        f"Switched session main model profile to '{profile_name}' ({profile.model})",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
    )
    return CommandResult(action="continue", payload=payload)


def _handle_use_main_model(command, ctx) -> CommandResult:
    return _handle_switch_model(SwitchModelCommand(profile_name=command.profile_name), ctx)


def _handle_use_sub_model(command, ctx) -> CommandResult:
    profile_name = command.profile_name
    profile = _resolve_profile(ctx, profile_name)
    if profile is None:
        return CommandResult(action="continue")

    setattr(ctx.agent, "active_sub_model_profile", profile_name)
    payload = _refresh_model_view(ctx)
    ctx.ui_bus.success(
        f"Switched session sub-agent model profile to '{profile_name}' ({profile.model})",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
    )
    return CommandResult(action="continue", payload=payload)


def _handle_set_main_model(command, ctx) -> CommandResult:
    profile_name = command.profile_name
    profile = _resolve_profile(ctx, profile_name)
    if profile is None:
        return CommandResult(action="continue")

    ctx.config.active_model_profile = profile_name
    ctx.config.active_main_model_profile = profile_name
    ctx.config.model = profile.model
    ctx.config.api_key = profile.api_key
    ctx.config.base_url = profile.base_url
    ctx.config.temperature = profile.temperature
    ctx.config.max_tokens = profile.max_tokens
    ctx.config.max_context_tokens = profile.max_context_tokens
    path = WorkspaceConfigStore().save_active_model_profile(profile_name)

    _apply_main_profile_to_runtime(ctx, profile_name, profile)
    payload = _refresh_model_view(ctx)
    ctx.ui_bus.success(
        f"Set global main model profile to '{profile_name}' ({profile.model}) and saved to {path}",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
        saved_path=str(path),
    )
    return CommandResult(action="continue", payload=payload)


def _handle_set_sub_model(command, ctx) -> CommandResult:
    profile_name = command.profile_name
    profiles = getattr(ctx.config, "model_profiles", {}) or {}
    profile = profiles.get(profile_name)
    if profile is None:
        ctx.ui_bus.error(
            f"Unknown model profile '{profile_name}'. Use /model to list available profiles.",
            kind=UIEventKind.MODEL,
            profile_name=profile_name,
        )
        return CommandResult(action="continue")

    ctx.config.active_sub_model_profile = profile_name
    path = WorkspaceConfigStore().save_active_sub_model_profile(profile_name)

    ctx.ui_bus.success(
        f"Set global sub-agent model profile to '{profile_name}' ({profile.model}) and saved to {path}",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
        saved_path=str(path),
    )

    payload = _refresh_model_view(ctx)

    return CommandResult(action="continue", payload=payload)


def _build_model_profiles_payload(config, runtime_state=None) -> dict:
    profiles = getattr(config, "model_profiles", {}) or {}
    runtime_main = getattr(runtime_state, "active_main_model_profile", None) if runtime_state is not None else None
    runtime_sub = getattr(runtime_state, "active_sub_model_profile", None) if runtime_state is not None else None
    runtime_model = getattr(runtime_state, "model", None) if runtime_state is not None else None
    active_main = runtime_main or getattr(config, "active_main_model_profile", None) or getattr(config, "active_model_profile", None)
    active_sub = runtime_sub or getattr(config, "active_sub_model_profile", None) or active_main

    lines: list[str] = ["## Model Routing"]
    profile_items: list[dict] = []

    if active_main:
        lines.append(f"- main agent: `{active_main}`")
    else:
        lines.append(f"- main agent: runtime `{runtime_model or config.model}`")
        if config.base_url:
            lines.append(f"  - base_url: `{config.base_url}`")

    if active_sub:
        lines.append(f"- sub-agent default: `{active_sub}`")
    else:
        lines.append("- sub-agent default: inherits main agent runtime")

    lines.append("")
    lines.append("**Commands**")
    lines.append("- `/model <profile>` or `/model use-main <profile>` → switch session main model")
    lines.append("- `/model use-sub <profile>` → switch session sub-agent model")
    lines.append("- `/model set-main <profile>` → set global default main model")
    lines.append("- `/model set-sub <profile>` → set global default sub-agent model")
    lines.append("- `agent(..., model=\"sub\"|\"main\")` → route a sub-agent to the configured sub/main model")
    lines.append("")

    if not profiles:
        lines.append("> No model profiles configured. Add `models.profiles` in config.yaml.")
        lines.append("")
        lines.append("**Current runtime config**")
        lines.append(f"- model: `{config.model}`")
        lines.append(f"- max_tokens: {config.max_tokens}")
        lines.append(f"- temperature: {config.temperature}")
        lines.append(f"- max_context_tokens: {config.max_context_tokens}")
    else:
        lines.append("## Profiles")
        lines.append("")
        for name in sorted(profiles):
            p = profiles[name]
            badges: list[str] = []
            if active_main == name:
                badges.append("MAIN")
            if active_sub == name:
                badges.append("SUB")
            badge_text = f" [{' | '.join(badges)}]" if badges else ""
            api_key = getattr(p, "api_key", "")
            if api_key and len(api_key) >= 4:
                api_hint = f"...{api_key[-4:]}"
            elif api_key:
                api_hint = f"...{api_key}"
            else:
                api_hint = "(empty)"

            item = {
                "name": name,
                "active": active_main == name,
                "active_main": active_main == name,
                "active_sub": active_sub == name,
                "model": p.model,
                "base_url": p.base_url,
                "max_tokens": p.max_tokens,
                "temperature": p.temperature,
                "max_context_tokens": p.max_context_tokens,
                "api_key_hint": api_hint,
            }
            profile_items.append(item)

            lines.append(f"### {name}{badge_text}")
            lines.append(f"- model: `{p.model}`")
            if p.base_url:
                lines.append(f"- base_url: `{p.base_url}`")
            lines.append(f"- max_tokens: {p.max_tokens}")
            lines.append(f"- temperature: {p.temperature}")
            lines.append(f"- max_context_tokens: {p.max_context_tokens}")
            lines.append(f"- api_key: `{api_hint}`")
            lines.append("")

    return {
        "active_profile": active_main,
        "active_main_profile": active_main,
        "active_sub_profile": active_sub,
        "current_model": runtime_model or config.model,
        "markdown": "\n".join(lines),
        "profiles": profile_items,
    }


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="model.show",
                feature_id="model",
                description="Show model profiles and current session/global routing",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model"),),
                parser=_parse_show_model,
                handler=_handle_show_model,
            ),
            ActionSpec(
                action_id="model.use_main",
                feature_id="model",
                description="[session] Use a session main model profile",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model use-main <profile>"),),
                parser=_parse_use_main_model,
                handler=_handle_use_main_model,
            ),
            ActionSpec(
                action_id="model.use_sub",
                feature_id="model",
                description="[session] Use a session sub-agent model profile",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model use-sub <profile>"),),
                parser=_parse_use_sub_model,
                handler=_handle_use_sub_model,
            ),
            ActionSpec(
                action_id="model.set_main",
                feature_id="model",
                description="[global] Set the global default main model profile",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model set-main <profile>"),),
                parser=_parse_set_main_model,
                handler=_handle_set_main_model,
            ),
            ActionSpec(
                action_id="model.set_sub",
                feature_id="model",
                description="[global] Set the global default sub-agent model profile",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model set-sub <profile>"),),
                parser=_parse_set_sub_model,
                handler=_handle_set_sub_model,
            ),
            ActionSpec(
                action_id="model.switch",
                feature_id="model",
                description="[session] Switch the session main model profile",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model <profile>"),),
                parser=_parse_switch_model,
                handler=_handle_switch_model,
            ),
        ]
    )
