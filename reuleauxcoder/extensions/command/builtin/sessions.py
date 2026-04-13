"""Builtin sessions command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import TEXT_REQUIRED, UI_TARGETS, non_empty_text, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class ListSessionsCommand:
    limit: int = 20


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
    sessions = store.list(limit=command.limit)
    payload = {
        "sessions": [
            {
                "id": s.id,
                "model": s.model,
                "saved_at": s.saved_at,
                "preview": s.preview,
            }
            for s in sessions
        ]
    }

    if not sessions:
        ctx.ui_bus.info("No saved sessions.", kind=UIEventKind.SESSION)
    else:
        ctx.ui_bus.open_view(
            "sessions",
            title="Saved Sessions",
            payload=payload,
            reuse_key="sessions",
        )

    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="sessions",
                title="Saved Sessions",
                payload=payload,
                reuse_key="sessions",
            )
        ]
        if sessions
        else [],
        payload=payload,
    )


def _handle_resume_session(command, ctx) -> CommandResult:
    if not command.target:
        ctx.ui_bus.error("Usage: /session <session_id|latest>", kind=UIEventKind.SESSION)
        return CommandResult(action="continue")

    store = SessionStore(ctx.sessions_dir)
    session_id = command.target
    if command.target == "latest":
        latest = store.get_latest()
        if latest is None:
            ctx.ui_bus.error("No saved sessions.", kind=UIEventKind.SESSION)
            return CommandResult(action="continue")
        session_id = latest.id

    loaded = store.load(session_id)
    if loaded is None:
        ctx.ui_bus.error(f"Session '{session_id}' not found.", kind=UIEventKind.SESSION)
        return CommandResult(action="continue")

    messages, loaded_model, prompt_tokens, completion_tokens = loaded
    ctx.agent.state.messages = list(messages)
    ctx.agent.state.total_prompt_tokens = prompt_tokens
    ctx.agent.state.total_completion_tokens = completion_tokens

    if loaded_model and loaded_model != ctx.config.model:
        ctx.agent.llm.model = loaded_model
        ctx.config.model = loaded_model
        ctx.ui_bus.info(
            f"Model switched to session model: {loaded_model}",
            kind=UIEventKind.SESSION,
            model=loaded_model,
        )

    exit_time = store.get_exit_time(messages)
    ctx.ui_bus.success(f"Resumed session: {session_id}", kind=UIEventKind.SESSION, session_id=session_id)

    return CommandResult(
        action="continue",
        session_id=session_id,
        session_exit_time=exit_time,
        payload={"session_id": session_id, "session_exit_time": exit_time},
    )


def _handle_save_session(command, ctx) -> CommandResult:
    store = SessionStore(ctx.sessions_dir)
    session_id = store.save(
        ctx.agent.messages,
        ctx.config.model,
        command.current_session_id,
        total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
        total_completion_tokens=ctx.agent.state.total_completion_tokens,
    )
    ctx.ui_bus.success(f"Session saved: {session_id}", kind=UIEventKind.SESSION, session_id=session_id)
    ctx.ui_bus.info(f"Resume with: rcoder -r {session_id}", kind=UIEventKind.SESSION, session_id=session_id)
    return CommandResult(action="continue", session_id=session_id, payload={"session_id": session_id})


def _handle_new_session(command, ctx) -> CommandResult:
    store = SessionStore(ctx.sessions_dir)
    previous_session_id = command.current_session_id
    if ctx.agent.messages:
        sid = store.save(
            ctx.agent.messages,
            ctx.config.model,
            previous_session_id,
            total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
            total_completion_tokens=ctx.agent.state.total_completion_tokens,
        )
        previous_session_id = sid
        ctx.ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION, session_id=sid)

    new_session_id = store.generate_session_id()
    ctx.agent.reset()
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
    return CommandResult(action="continue", session_id=new_session_id, session_exit_time=None)


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="sessions.list",
                feature_id="sessions",
                description="List saved sessions",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/sessions"),),
                parser=_parse_list_sessions,
                handler=_handle_list_sessions,
            ),
            ActionSpec(
                action_id="sessions.resume",
                feature_id="sessions",
                description="Resume a session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/session <id|latest>"),),
                parser=_parse_resume_session,
                handler=_handle_resume_session,
            ),
            ActionSpec(
                action_id="sessions.save",
                feature_id="sessions",
                description="Save current session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/save"),),
                parser=_parse_save_session,
                handler=_handle_save_session,
            ),
            ActionSpec(
                action_id="sessions.new",
                feature_id="sessions",
                description="Start a new session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/new"),),
                parser=_parse_new_session,
                handler=_handle_new_session,
            ),
        ]
    )
