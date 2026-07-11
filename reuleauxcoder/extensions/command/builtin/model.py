"""Builtin model command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.view_models import (
    ModelListViewModel,
    ModelProfileViewModel,
)
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
from reuleauxcoder.infrastructure.persistence.workspace_config_store import (
    WorkspaceConfigStore,
)
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.services.llm.factory import reconfigure_llm_from_settings


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
            reject=frozenset(
                {"ls", "list", "show", "use-main", "use-sub", "set-main", "set-sub"}
            )
        ).parse(captures["profile"])
    except ParamParseError:
        return None

    return SwitchModelCommand(profile_name=profile)


def _handle_show_model(command, ctx) -> CommandResult:
    view = _build_model_profiles_view(
        ctx.config,
        runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
    )

    ctx.ui_bus.open_view(
        view.view_type,
        title="Model Profiles",
        view_model=view,
        reuse_key="model_profiles",
    )

    return CommandResult(action="continue", payload=view.to_payload())


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
    debug_trace = getattr(
        ctx.agent.llm, "debug_trace", getattr(ctx.config, "llm_debug_trace", False)
    )
    reconfigure_llm_from_settings(
        ctx.agent.llm,
        profile,
        debug_trace=debug_trace,
    )
    ctx.agent.context.reconfigure(profile.max_context_tokens)
    ctx.agent.active_main_model_profile = profile_name


def _refresh_model_view(ctx) -> ModelListViewModel:
    view = _build_model_profiles_view(
        ctx.config,
        runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
    )
    ctx.ui_bus.refresh_view(
        view.view_type,
        title="Model Profiles",
        view_model=view,
        reuse_key="model_profiles",
    )
    return view


def _handle_switch_model(command, ctx) -> CommandResult:
    profile_name = command.profile_name
    profile = _resolve_profile(ctx, profile_name)
    if profile is None:
        return CommandResult(action="continue")

    _apply_main_profile_to_runtime(ctx, profile_name, profile)
    view = _refresh_model_view(ctx)
    ctx.ui_bus.success(
        f"Switched session main model profile to '{profile_name}' ({profile.model})",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
    )
    return CommandResult(action="continue", payload=view.to_payload())


def _handle_use_main_model(command, ctx) -> CommandResult:
    return _handle_switch_model(
        SwitchModelCommand(profile_name=command.profile_name), ctx
    )


def _handle_use_sub_model(command, ctx) -> CommandResult:
    profile_name = command.profile_name
    profile = _resolve_profile(ctx, profile_name)
    if profile is None:
        return CommandResult(action="continue")

    ctx.agent.active_sub_model_profile = profile_name
    view = _refresh_model_view(ctx)
    ctx.ui_bus.success(
        f"Switched session sub-agent model profile to '{profile_name}' ({profile.model})",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
    )
    return CommandResult(action="continue", payload=view.to_payload())


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
    view = _refresh_model_view(ctx)
    ctx.ui_bus.success(
        f"Set global main model profile to '{profile_name}' ({profile.model}) and saved to {path}",
        kind=UIEventKind.MODEL,
        profile_name=profile_name,
        model=profile.model,
        saved_path=str(path),
    )
    return CommandResult(action="continue", payload=view.to_payload())


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

    view = _refresh_model_view(ctx)

    return CommandResult(action="continue", payload=view.to_payload())


def _build_model_profiles_view(config, runtime_state=None) -> ModelListViewModel:
    profiles = getattr(config, "model_profiles", {}) or {}
    runtime_main = (
        getattr(runtime_state, "active_main_model_profile", None)
        if runtime_state is not None
        else None
    )
    runtime_sub = (
        getattr(runtime_state, "active_sub_model_profile", None)
        if runtime_state is not None
        else None
    )
    runtime_model = (
        getattr(runtime_state, "model", None) if runtime_state is not None else None
    )
    active_main = (
        runtime_main
        or getattr(config, "active_main_model_profile", None)
        or getattr(config, "active_model_profile", None)
    )
    active_sub = (
        runtime_sub or getattr(config, "active_sub_model_profile", None) or active_main
    )

    profile_items = []
    for name in sorted(profiles):
        profile = profiles[name]
        api_key = getattr(profile, "api_key", "")
        if api_key and len(api_key) >= 4:
            api_hint = f"...{api_key[-4:]}"
        elif api_key:
            api_hint = f"...{api_key}"
        else:
            api_hint = "(empty)"
        profile_items.append(
            ModelProfileViewModel(
                name=name,
                model=profile.model,
                active_main=active_main == name,
                active_sub=active_sub == name,
                base_url=profile.base_url,
                max_tokens=profile.max_tokens,
                temperature=profile.temperature,
                max_context_tokens=profile.max_context_tokens,
                api_key_hint=api_hint,
            )
        )
    diagnostics = (
        ("No model profiles configured. Add models.profiles in config.yaml.",)
        if not profiles
        else ()
    )
    return ModelListViewModel(
        active_main=active_main,
        active_sub=active_sub,
        current_model=runtime_model or config.model,
        profiles=tuple(profile_items),
        diagnostics=diagnostics,
    )


def _build_model_profiles_payload(config, runtime_state=None) -> dict:
    """Serializable compatibility projection for callers outside presentation."""
    return _build_model_profiles_view(config, runtime_state).to_payload()


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
