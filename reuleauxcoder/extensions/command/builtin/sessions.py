"""Builtin sessions command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    build_session_runtime_state,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.domain.hooks import HookPoint, SessionSaveContext
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class ListSessionsCommand:
    limit: int = 20
    show_all: bool = False


@dataclass(frozen=True, slots=True)
class ResumeSessionCommand:
    target: str


@dataclass(frozen=True, slots=True)
class SaveSessionCommand:
    current_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class NewSessionCommand:
    current_session_id: str | None = None


def _parse_list_sessions(user_input: str, parse_ctx):
    if match_template(user_input, "/sessions all") is not None:
        return ListSessionsCommand(show_all=True)
    if match_template(user_input, "/sessions") is not None:
        return ListSessionsCommand()
    return None


def _parse_resume_session(user_input: str, parse_ctx):
    captures = match_template(user_input, "/session {target+}")
    if captures is None:
        return None

    try:
        target = non_empty_text().parse(captures["target"])
    except ParamParseError:
        return ResumeSessionCommand(target="")

    return ResumeSessionCommand(target=target)


def _parse_save_session(user_input: str, parse_ctx):
    if match_template(user_input, "/save") is not None:
        return SaveSessionCommand(current_session_id=parse_ctx.current_session_id)
    return None


def _parse_new_session(user_input: str, parse_ctx):
    if match_template(user_input, "/new") is not None:
        return NewSessionCommand(current_session_id=parse_ctx.current_session_id)
    return None


def _handle_list_sessions(command, ctx) -> CommandResult:
    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    filter_fingerprint = None if command.show_all else fingerprint
    sessions = store.list(limit=command.limit, fingerprint=filter_fingerprint)
    payload = {
        "fingerprint": fingerprint,
        "show_all": command.show_all,
        "sessions": [
            {
                "id": s.id,
                "model": s.model,
                "saved_at": s.saved_at,
                "preview": s.preview,
                "fingerprint": s.fingerprint,
            }
            for s in sessions
        ],
    }

    if not sessions:
        if command.show_all:
            ctx.ui_bus.info(
                "No saved sessions across all fingerprints.",
                kind=UIEventKind.SESSION,
                fingerprint=fingerprint,
                show_all=True,
            )
        else:
            ctx.ui_bus.info(
                f"No saved sessions for fingerprint: {fingerprint}",
                kind=UIEventKind.SESSION,
                fingerprint=fingerprint,
            )
    else:
        ctx.ui_bus.open_view(
            "sessions",
            title="Saved Sessions",
            payload=payload,
            reuse_key="sessions",
        )

    return CommandResult(action="continue", payload=payload)


def _handle_resume_session(command, ctx) -> CommandResult:
    if not command.target:
        ctx.ui_bus.error(
            "Usage: /session <session_id|latest>", kind=UIEventKind.SESSION
        )
        return CommandResult(action="continue")

    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    session_id = command.target
    if command.target == "latest":
        latest = store.get_latest(fingerprint=fingerprint)
        if latest is None:
            ctx.ui_bus.error(
                f"No saved sessions for fingerprint: {fingerprint}",
                kind=UIEventKind.SESSION,
                fingerprint=fingerprint,
            )
            return CommandResult(action="continue")
        session_id = latest.id

    loaded = store.load(session_id)
    if loaded is None:
        ctx.ui_bus.error(f"Session '{session_id}' not found.", kind=UIEventKind.SESSION)
        return CommandResult(action="continue")

    if loaded.fingerprint != fingerprint:
        ctx.ui_bus.warning(
            f"Session '{session_id}' belongs to fingerprint '{loaded.fingerprint}', current fingerprint is '{fingerprint}'.",
            kind=UIEventKind.SESSION,
            session_id=session_id,
            fingerprint=loaded.fingerprint,
            current_fingerprint=fingerprint,
        )

    apply_session_runtime_state(loaded, ctx.config, ctx.agent)
    setattr(ctx.agent, "session_fingerprint", loaded.fingerprint)

    runtime = loaded.runtime_state
    if runtime.active_mode:
        ctx.ui_bus.info(
            f"Mode restored from session: {runtime.active_mode}",
            kind=UIEventKind.SESSION,
            mode_name=runtime.active_mode,
        )
    if runtime.model:
        ctx.ui_bus.info(
            f"Model restored from session: {runtime.model}",
            kind=UIEventKind.SESSION,
            model=runtime.model,
        )

    exit_time = store.get_exit_time(loaded.messages)
    ctx.ui_bus.success(
        f"Resumed session: {session_id}",
        kind=UIEventKind.SESSION,
        session_id=session_id,
    )

    return CommandResult(
        action="continue",
        session_id=session_id,
        session_exit_time=exit_time,
        payload={"session_id": session_id, "session_exit_time": exit_time},
    )


def _handle_save_session(command, ctx) -> CommandResult:
    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    session_id = store.save(
        ctx.agent.messages,
        getattr(ctx.agent.llm, "model", ctx.config.model),
        command.current_session_id,
        total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
        total_completion_tokens=ctx.agent.state.total_completion_tokens,
        active_mode=getattr(ctx.agent, "active_mode", None),
        runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
        fingerprint=fingerprint,
    )
    _emit_session_save_hooks(ctx.agent, session_id)
    ctx.ui_bus.success(
        f"Session saved: {session_id}", kind=UIEventKind.SESSION, session_id=session_id
    )
    ctx.ui_bus.info(
        f"Resume with: rcoder -r {session_id}",
        kind=UIEventKind.SESSION,
        session_id=session_id,
    )
    return CommandResult(
        action="continue",
        session_id=session_id,
        payload={"session_id": session_id, "fingerprint": fingerprint},
    )


def _handle_new_session(command, ctx) -> CommandResult:
    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    previous_session_id = command.current_session_id
    if ctx.agent.messages:
        sid = store.save(
            ctx.agent.messages,
            getattr(ctx.agent.llm, "model", ctx.config.model),
            previous_session_id,
            total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
            total_completion_tokens=ctx.agent.state.total_completion_tokens,
            active_mode=getattr(ctx.agent, "active_mode", None),
            runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
            fingerprint=fingerprint,
        )
        previous_session_id = sid
        _emit_session_save_hooks(ctx.agent, sid)
        ctx.ui_bus.info(
            f"Session auto-saved: {sid}", kind=UIEventKind.SESSION, session_id=sid
        )

    new_session_id = store.generate_session_id()
    ctx.agent.reset()
    restore_config_runtime_defaults(ctx.config, ctx.agent)
    setattr(ctx.agent, "session_fingerprint", fingerprint)
    ctx.ui_bus.success(
        f"Started a new conversation: {new_session_id}",
        kind=UIEventKind.SESSION,
        session_id=new_session_id,
    )
    if previous_session_id:
        ctx.ui_bus.info(
            f"Resume previous with: /session {previous_session_id}",
            kind=UIEventKind.SESSION,
            session_id=previous_session_id,
        )
    return CommandResult(
        action="continue", session_id=new_session_id, session_exit_time=None
    )


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="sessions.list",
                feature_id="sessions",
                description="[session-index] List saved sessions for the current fingerprint by default, or all with `/sessions all`",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/sessions"), slash_trigger("/sessions all")),
                parser=_parse_list_sessions,
                handler=_handle_list_sessions,
            ),
            ActionSpec(
                action_id="sessions.resume",
                feature_id="sessions",
                description="[session-index] Resume a saved session by id or latest visible fingerprint match",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/session <id|latest>"),),
                parser=_parse_resume_session,
                handler=_handle_resume_session,
            ),
            ActionSpec(
                action_id="sessions.save",
                feature_id="sessions",
                description="[session] Save the current session with its runtime overrides and fingerprint",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/save"),),
                parser=_parse_save_session,
                handler=_handle_save_session,
            ),
            ActionSpec(
                action_id="sessions.new",
                feature_id="sessions",
                description="[session] Start a new session after auto-saving the current one",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/new"),),
                parser=_parse_new_session,
                handler=_handle_new_session,
            ),
        ]
    )


def _emit_session_save_hooks(agent, session_id: str) -> None:
    """Emit SESSION_SAVE lifecycle hooks."""
    context = SessionSaveContext(
        hook_point=HookPoint.SESSION_SAVE,
        session_id=session_id,
    )
    for decision in agent.hook_registry.run_guards(HookPoint.SESSION_SAVE, context):
        if not decision.allowed:
            break
    agent.hook_registry.run_transforms(HookPoint.SESSION_SAVE, context)
    agent.hook_registry.run_observers(HookPoint.SESSION_SAVE, context)
