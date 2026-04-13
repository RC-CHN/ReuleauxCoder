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
from reuleauxcoder.infrastructure.persistence.workspace_config_store import WorkspaceConfigStore
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class SwitchModelCommand:
    profile_name: str


def _parse_show_model(user_input: str, parse_ctx):
    if matches_any(user_input, ("/model", "/model ls", "/model list", "/model show")):
        return EmptyCommand()
    return None


def _parse_switch_model(user_input: str, parse_ctx):
    captures = match_template(user_input, "/model {profile+}")
    if captures is None:
        return None

    try:
        profile = non_empty_text(reject=frozenset({"ls", "list", "show"})).parse(captures["profile"])
    except ParamParseError:
        return None

    return SwitchModelCommand(profile_name=profile)


def _handle_show_model(command, ctx) -> CommandResult:
    payload = _build_model_profiles_payload(ctx.config)

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


def _handle_switch_model(command, ctx) -> CommandResult:
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

    ctx.agent.llm.reconfigure(
        model=profile.model,
        api_key=profile.api_key,
        base_url=profile.base_url,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
    )
    ctx.config.model = profile.model
    ctx.config.api_key = profile.api_key
    ctx.config.base_url = profile.base_url
    ctx.config.temperature = profile.temperature
    ctx.config.max_tokens = profile.max_tokens
    ctx.config.max_context_tokens = profile.max_context_tokens
    ctx.config.active_model_profile = profile_name

    ctx.agent.context.reconfigure(profile.max_context_tokens)

    path = WorkspaceConfigStore().save_active_model_profile(profile_name)

    ctx.ui_bus.success(
        f"Switched model profile to '{profile_name}' ({profile.model}) and saved to {path}",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
        saved_path=str(path),
    )

    payload = _build_model_profiles_payload(ctx.config)
    ctx.ui_bus.refresh_view(
        "model_profiles",
        title="Model Profiles",
        payload=payload,
        reuse_key="model_profiles",
    )

    return CommandResult(action="continue", payload=payload)


def _build_model_profiles_payload(config) -> dict:
    profiles = getattr(config, "model_profiles", {}) or {}
    active = getattr(config, "active_model_profile", None)

    lines: list[str] = []
    profile_items: list[dict] = []

    if active:
        lines.append(f"**Current active profile:** `{active}`")
    else:
        lines.append(f"**Current provider:** `{config.model}`")
        if config.base_url:
            lines.append(f"  - base_url: `{config.base_url}`")

    lines.append("")

    if not profiles:
        lines.append("> No model profiles configured. Add `models.profiles` in config.yaml.")
        lines.append("")
        lines.append("**Runtime config:**")
        lines.append(f"  - max_tokens: {config.max_tokens}")
        lines.append(f"  - temperature: {config.temperature}")
        lines.append(f"  - max_context_tokens: {config.max_context_tokens}")
    else:
        lines.append("**Model profiles:**")
        lines.append("")
        for name in sorted(profiles):
            p = profiles[name]
            marker = " ✓" if active == name else ""
            api_key = getattr(p, "api_key", "")
            if api_key and len(api_key) >= 4:
                api_hint = f"...{api_key[-4:]}"
            elif api_key:
                api_hint = f"...{api_key}"
            else:
                api_hint = "(empty)"

            item = {
                "name": name,
                "active": active == name,
                "model": p.model,
                "base_url": p.base_url,
                "max_tokens": p.max_tokens,
                "temperature": p.temperature,
                "max_context_tokens": p.max_context_tokens,
                "api_key_hint": api_hint,
            }
            profile_items.append(item)

            lines.append(f"- **{name}**{marker}")
            lines.append(f"  - model: `{p.model}`")
            if p.base_url:
                lines.append(f"  - base_url: `{p.base_url}`")
            lines.append(f"  - max_tokens: {p.max_tokens}")
            lines.append(f"  - temperature: {p.temperature}")
            lines.append(f"  - max_context_tokens: {p.max_context_tokens}")
            lines.append(f"  - api_key: `{api_hint}`")
            lines.append("")

    return {
        "active_profile": active,
        "current_model": config.model,
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
                description="Show model profiles",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model"),),
                parser=_parse_show_model,
                handler=_handle_show_model,
            ),
            ActionSpec(
                action_id="model.switch",
                feature_id="model",
                description="Switch active model profile",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/model <profile>"),),
                parser=_parse_switch_model,
                handler=_handle_switch_model,
            ),
        ]
    )
