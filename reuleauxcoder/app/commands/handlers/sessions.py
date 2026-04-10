"""Shared handlers for session-related command families."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import (
    CommandContext,
    CommandResult,
    ListSessionsCommand,
    NewSessionCommand,
    OpenViewRequest,
    ResumeSessionCommand,
    SaveSessionCommand,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.events import UIEventKind


def handle_list_sessions(command: ListSessionsCommand, ctx: CommandContext) -> CommandResult:
    """List saved sessions and publish a structured sessions view."""
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


def handle_resume_session(command: ResumeSessionCommand, ctx: CommandContext) -> CommandResult:
    """Resume a saved session by ID or `latest`."""
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


def handle_save_session(command: SaveSessionCommand, ctx: CommandContext) -> CommandResult:
    """Persist the current in-memory conversation."""
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


def handle_new_session(command: NewSessionCommand, ctx: CommandContext) -> CommandResult:
    """Start a new conversation, auto-saving the previous one when needed."""
    previous_session_id = command.current_session_id
    if ctx.agent.messages:
        sid = SessionStore(ctx.sessions_dir).save(
            ctx.agent.messages,
            ctx.config.model,
            previous_session_id,
            total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
            total_completion_tokens=ctx.agent.state.total_completion_tokens,
        )
        previous_session_id = sid
        ctx.ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION, session_id=sid)

    ctx.agent.reset()
    ctx.ui_bus.success("Started a new conversation.", kind=UIEventKind.SESSION)
    if previous_session_id:
        ctx.ui_bus.info(
            f"Resume previous with: /session {previous_session_id}",
            kind=UIEventKind.SESSION,
            session_id=previous_session_id,
        )
    return CommandResult(action="continue", session_id=None, session_exit_time=None)
