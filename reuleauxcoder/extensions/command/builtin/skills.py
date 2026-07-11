"""Builtin skills command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandEffect
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
from reuleauxcoder.extensions.skills.models import SkillsSummary, SkillsViewModel
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class ToggleSkillCommand:
    skill_name: str
    enabled: bool


def _parse_show_skills(user_input: str, parse_ctx):
    if matches_any(user_input, ("/skills", "/skills show")):
        return EmptyCommand()
    return None


def _parse_reload_skills(user_input: str, parse_ctx):
    if match_template(user_input, "/skills reload") is not None:
        return EmptyCommand()
    return None


def _parse_enable_skill(user_input: str, parse_ctx):
    captures = match_template(user_input, "/skills enable {name+}")
    if captures is None:
        return None
    try:
        skill_name = non_empty_text().parse(captures["name"])
    except ParamParseError:
        return None
    return ToggleSkillCommand(skill_name=skill_name, enabled=True)


def _parse_disable_skill(user_input: str, parse_ctx):
    captures = match_template(user_input, "/skills disable {name+}")
    if captures is None:
        return None
    try:
        skill_name = non_empty_text().parse(captures["name"])
    except ParamParseError:
        return None
    return ToggleSkillCommand(skill_name=skill_name, enabled=False)


def _build_reload_message(result) -> str:
    parts = [
        f"{len(result.all_skills)} discovered",
        f"{len(result.active_skills)} active",
    ]
    if result.added:
        parts.append(f"+{len(result.added)} added")
    if result.updated:
        parts.append(f"~{len(result.updated)} updated")
    if result.removed:
        parts.append(f"-{len(result.removed)} removed")
    if result.missing:
        parts.append(f"{len(result.missing)} missing")
    return "Skills reloaded: " + ", ".join(parts) + "."


def _build_skills_view(ctx) -> SkillsViewModel:
    service = ctx.skills_service
    if service is None:
        return SkillsViewModel(
            skills=(),
            summary=SkillsSummary(0, 0, 0, False, False, False, False),
        )
    return service.build_view()


def _handle_show_skills(command, ctx) -> CommandEffect:
    view = _build_skills_view(ctx)
    ctx.effect.open_view(
        view.view_type,
        title="Skills",
        view_model=view,
        reuse_key="skills",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_reload_skills(command, ctx) -> CommandEffect:
    service = ctx.skills_service
    if service is None:
        ctx.effect.error("Skills service unavailable.", kind=UIEventKind.SYSTEM)
        return ctx.effect.finish(control="continue")

    result = service.reload()
    ctx.agent.skills_catalog = result.catalog
    ctx.effect.success(
        _build_reload_message(result),
        kind=UIEventKind.SYSTEM,
    )
    for name in result.added:
        ctx.effect.info(f"Skill added: {name}", kind=UIEventKind.SYSTEM)
    for name in result.updated:
        ctx.effect.info(f"Skill updated: {name}", kind=UIEventKind.SYSTEM)
    for name in result.removed:
        ctx.effect.warning(f"Skill removed: {name}", kind=UIEventKind.SYSTEM)
    for name in result.missing:
        ctx.effect.warning(
            f"Skill not found and skipped: {name}", kind=UIEventKind.SYSTEM
        )
    for diagnostic in result.diagnostics:
        emit = ctx.effect.warning if diagnostic.level == "warning" else ctx.effect.error
        emit(diagnostic.message, kind=UIEventKind.SYSTEM)

    view = _build_skills_view(ctx)
    ctx.effect.refresh_view(
        view.view_type,
        title="Skills",
        view_model=view,
        reuse_key="skills",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_toggle_skill(command: ToggleSkillCommand, ctx) -> CommandEffect:
    service = ctx.skills_service
    if service is None:
        ctx.effect.error("Skills service unavailable.", kind=UIEventKind.SYSTEM)
        return ctx.effect.finish(control="continue")

    result = service.set_enabled(command.skill_name, command.enabled)
    if not result.found:
        ctx.effect.warning(
            result.message, kind=UIEventKind.SYSTEM, skill_name=command.skill_name
        )
        return ctx.effect.finish(control="continue")

    ctx.agent.skills_catalog = service.build_catalog()
    if result.changed:
        if hasattr(ctx.config, "skills"):
            ctx.config.skills.disabled = list(service.disabled_names)
        ctx.effect.success(
            result.message,
            kind=UIEventKind.SYSTEM,
            skill_name=command.skill_name,
            saved_path=result.saved_path,
        )
    else:
        ctx.effect.info(
            result.message, kind=UIEventKind.SYSTEM, skill_name=command.skill_name
        )

    view = _build_skills_view(ctx)
    ctx.effect.refresh_view(
        view.view_type,
        title="Skills",
        view_model=view,
        reuse_key="skills",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="skills.show",
                feature_id="skills",
                description="Show available skills and global enable/disable state",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/skills"),),
                parser=_parse_show_skills,
                handler=_handle_show_skills,
            ),
            ActionSpec(
                action_id="skills.reload",
                feature_id="skills",
                description="[global] Reload skills from disk into the current process",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/skills reload"),),
                parser=_parse_reload_skills,
                handler=_handle_reload_skills,
            ),
            ActionSpec(
                action_id="skills.enable",
                feature_id="skills",
                description="[global] Enable a skill in workspace config",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/skills enable <name>"),),
                parser=_parse_enable_skill,
                handler=_handle_toggle_skill,
            ),
            ActionSpec(
                action_id="skills.disable",
                feature_id="skills",
                description="[global] Disable a skill in workspace config",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/skills disable <name>"),),
                parser=_parse_disable_skill,
                handler=_handle_toggle_skill,
            ),
        ]
    )
