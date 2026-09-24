"""Builtin sessions command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.shared import (
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.commands.view_models import (
    SessionResumeViewModel,
    SessionTranscriptEntryViewModel,
    SessionsViewModel,
    SessionSummaryViewModel,
)
from reuleauxcoder.app.runtime.session_state import (
    bind_session_persistence,
    apply_session_runtime_state,
    save_session_snapshot,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.infrastructure.persistence.session_store import (
    SessionRestoreError,
    SessionStore,
)
from reuleauxcoder.app.ui_events import UIEventKind


@dataclass(frozen=True, slots=True)
class ListSessionsCommand:
    limit: int = 20
    show_all: bool = False


@dataclass(frozen=True, slots=True)
class ResumeSessionCommand:
    target: str


@dataclass(frozen=True, slots=True)
class SaveSessionCommand:
    pass


@dataclass(frozen=True, slots=True)
class NewSessionCommand:
    pass


def _settle_current_session(ctx, store, session_id: str | None, fingerprint: str):
    """Save once: settle a live callback, or fall back to an explicit snapshot."""
    callback = getattr(ctx.agent, "_session_persist_callback", None)
    unbind = getattr(ctx.agent, "unbind_session_persistence", None)
    if callback is not None and callable(unbind):
        unbind()
        return session_id
    return _save_session_snapshot(ctx, store, session_id, fingerprint)


def _save_session_snapshot(ctx, store, session_id: str | None, fingerprint: str):
    return save_session_snapshot(
        ctx.config, ctx.agent, store, session_id,
        fingerprint=fingerprint,
    )


def _parse_list_sessions(user_input: str, parse_ctx):
    if (
        match_template(user_input, "/session all") is not None
        or match_template(user_input, "/sessions all") is not None
    ):
        return ListSessionsCommand(show_all=True)
    if (
        match_template(user_input, "/sessions") is not None
        or match_template(user_input, "/session") is not None
    ):
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
        return SaveSessionCommand()
    return None


def _parse_new_session(user_input: str, parse_ctx):
    if match_template(user_input, "/new") is not None:
        return NewSessionCommand()
    return None


def _report_session_inventory(ctx, issues: tuple) -> tuple:
    issues = tuple(issues)
    ctx.agent.session_inventory_issues = issues
    for issue in issues:
        ctx.effect.warning(
            f"Session inventory degraded ({issue.render()}).",
            kind=UIEventKind.SESSION,
            phase=issue.phase,
            error_type=issue.error_type,
            ref=issue.ref,
            count=issue.count,
        )
    return issues


def _handle_list_sessions(command, ctx) -> CommandEffect:
    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    filter_fingerprint = None if command.show_all else fingerprint
    inventory = store.list_result(limit=command.limit, fingerprint=filter_fingerprint)
    sessions = inventory.sessions
    _report_session_inventory(ctx, inventory.issues)
    current_session_id = getattr(ctx.agent, "current_session_id", None)
    view = SessionsViewModel(
        fingerprint=fingerprint,
        show_all=command.show_all,
        sessions=tuple(
            SessionSummaryViewModel(
                position=index if not command.show_all else None,
                session_id=session.id,
                model=session.model,
                saved_at=session.saved_at,
                preview=session.preview,
                fingerprint=session.fingerprint,
                active=session.id == current_session_id,
            )
            for index, session in enumerate(sessions, start=1)
        ),
    )
    ctx.effect.open_view(
        view,
        title="Saved Sessions",
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_resume_session(command, ctx) -> CommandEffect:
    try:
        return _resume_session(command, ctx)
    except SessionRestoreError as error:
        ctx.agent.record_runtime_issue(error.phase, error.error_type, error.ref)
        ctx.effect.error(
            str(error),
            kind=UIEventKind.SESSION,
            phase=error.phase,
            error_type=error.error_type,
            ref=error.ref,
        )
        return ctx.effect.finish(control="continue")


def _resume_session(command, ctx) -> CommandEffect:
    if not command.target:
        ctx.effect.error(
            "Usage: /session <number|session_id|latest>; use /session to list.",
            kind=UIEventKind.SESSION,
        )
        return ctx.effect.finish(control="continue")

    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    session_id = command.target
    inventory_issues = ()
    if command.target.isdecimal():
        position = int(command.target)
        inventory = store.list_result(limit=20, fingerprint=fingerprint)
        sessions = inventory.sessions
        inventory_issues = _report_session_inventory(ctx, inventory.issues)
        if position < 1 or position > len(sessions):
            ctx.effect.error(
                f"Session number {position} is not available; use /session to list.",
                kind=UIEventKind.SESSION,
            )
            return ctx.effect.finish(control="continue")
        session_id = sessions[position - 1].id
    elif command.target == "latest":
        inventory = store.get_latest_result(fingerprint=fingerprint)
        latest = inventory.session
        inventory_issues = _report_session_inventory(ctx, inventory.issues)
        if latest is None:
            ctx.effect.error(
                f"No saved sessions for fingerprint: {fingerprint}",
                kind=UIEventKind.SESSION,
                fingerprint=fingerprint,
            )
            return ctx.effect.finish(control="continue")
        session_id = latest.id

    loaded = store.load(session_id)
    if loaded is None:
        raise SessionRestoreError(
            phase="session_discovery",
            error_type="FileNotFoundError",
            ref="session",
        ) from None

    if loaded.fingerprint != fingerprint:
        ctx.effect.warning(
            f"Session '{session_id}' belongs to fingerprint '{loaded.fingerprint}', current fingerprint is '{fingerprint}'.",
            kind=UIEventKind.SESSION,
            session_id=session_id,
            fingerprint=loaded.fingerprint,
            current_fingerprint=fingerprint,
        )

    current_session_id = ctx.agent.current_session_id
    if (
        current_session_id
        and current_session_id != session_id
        and ctx.agent.messages
        and ctx.config.session_auto_save
    ):
        saved_id = _settle_current_session(
            ctx,
            store,
            current_session_id,
            fingerprint,
        )
        ctx.agent.lifecycle.session_saved(saved_id)

    # A filesystem preflight failure still belongs to the old live session.
    events_path = store.get_session_events_path(session_id)
    exit_time = store.get_exit_time(loaded.messages)

    apply_session_runtime_state(loaded, ctx.config, ctx.agent)
    ctx.agent.session_inventory_issues = tuple(inventory_issues)
    ctx.agent.session_fingerprint = loaded.fingerprint
    bind_issue = bind_session_persistence(
        ctx.config,
        ctx.agent,
        store,
        session_id,
        fingerprint=loaded.fingerprint,
        events_path=events_path,
    )
    ctx.agent.lifecycle.session_started(session_id, reason="restore")

    runtime = loaded.runtime_state
    restore_issues = tuple(getattr(loaded, "restore_issues", ()))
    for issue in restore_issues:
        ctx.effect.warning(
            f"Session restored with degraded state ({issue.render()}).",
            kind=UIEventKind.SESSION,
            phase=issue.phase,
            error_type=issue.error_type,
            ref=issue.ref,
            count=issue.count,
        )
    if bind_issue is not None:
        ctx.effect.warning(
            "Session restored, but persistence is unavailable "
            f"({bind_issue.render()}).",
            kind=UIEventKind.SESSION,
            phase=bind_issue.phase,
            error_type=bind_issue.error_type,
            ref=bind_issue.ref,
        )
    restored_notice = (
        ctx.effect.warning if restore_issues or bind_issue else ctx.effect.success
    )
    restored_notice(
        (
            f"Resumed session with degraded recovery: {session_id}"
            if restore_issues or bind_issue
            else f"Resumed session: {session_id}"
        ),
        kind=UIEventKind.SESSION,
        session_id=session_id,
    )
    transcript = SessionResumeViewModel(
        session_id=session_id,
        model=runtime.model or loaded.model,
        saved_at=loaded.saved_at,
        active_mode=runtime.active_mode,
        entries=tuple(
            SessionTranscriptEntryViewModel(
                role=entry["role"], content=entry["content"]
            )
            for entry in loaded.get_recent_conversation(max_user_turns=3)
        ),
    )
    ctx.effect.open_view(
        transcript,
        title="Recent Session Context",
        reuse_key=transcript.view_type,
    )

    return ctx.effect.finish(
        control="continue",
        session_id=session_id,
        session_exit_time=exit_time,
        state_changes={"session_id": session_id, "session_exit_time": exit_time},
    )


def _handle_save_session(command, ctx) -> CommandEffect:
    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    session_id = _save_session_snapshot(
        ctx, store, ctx.agent.current_session_id, fingerprint
    )
    ctx.agent.lifecycle.session_saved(session_id)
    ctx.effect.success(
        f"Session saved: {session_id}", kind=UIEventKind.SESSION, session_id=session_id
    )
    ctx.effect.info(
        f"Resume with: rcoder -r {session_id}",
        kind=UIEventKind.SESSION,
        session_id=session_id,
    )
    return ctx.effect.finish(
        control="continue",
        session_id=session_id,
        state_changes={"session_id": session_id, "fingerprint": fingerprint},
    )


def _handle_new_session(command, ctx) -> CommandEffect:
    ctx.effect.clear_transcript = True
    store = SessionStore(ctx.sessions_dir)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    previous_session_id = ctx.agent.current_session_id
    if ctx.agent.messages and ctx.config.session_auto_save:
        sid = _settle_current_session(
            ctx,
            store,
            previous_session_id,
            fingerprint,
        )
        previous_session_id = sid
        ctx.agent.lifecycle.session_saved(sid)
        ctx.effect.info(
            f"Session auto-saved: {sid}", kind=UIEventKind.SESSION, session_id=sid
        )

    new_session_id = store.generate_session_id()
    events_path = store.get_session_events_path(new_session_id)
    unbind = getattr(ctx.agent, "unbind_session_persistence", None)
    if callable(unbind):
        unbind()
    ctx.agent.reset()
    start_new_history = getattr(ctx.agent, "start_new_history", None)
    if callable(start_new_history):
        start_new_history()
    restore_config_runtime_defaults(ctx.config, ctx.agent)
    ctx.agent.session_fingerprint = fingerprint
    bind_issue = bind_session_persistence(
        ctx.config,
        ctx.agent,
        store,
        new_session_id,
        fingerprint=fingerprint,
        events_path=events_path,
    )
    ctx.agent.lifecycle.session_started(new_session_id, reason="new")
    notice = ctx.effect.warning if bind_issue is not None else ctx.effect.success
    notice(
        (
            f"Started a new conversation without persistence: {new_session_id}"
            if bind_issue is not None
            else f"Started a new conversation: {new_session_id}"
        ),
        kind=UIEventKind.SESSION,
        session_id=new_session_id,
        persistence_issue=(bind_issue.to_dict() if bind_issue is not None else None),
    )
    if previous_session_id:
        ctx.effect.info(
            f"Resume previous with: /session {previous_session_id}",
            kind=UIEventKind.SESSION,
            session_id=previous_session_id,
        )
    return ctx.effect.finish(
        control="continue", session_id=new_session_id, session_exit_time=None
    )


def command_panel_spec() -> CommandPanelSpec:
    """Contribute the restorable-session picker with canonical resume commands."""

    def build(model: object, title: str) -> PanelDefinition:
        assert isinstance(model, SessionsViewModel)
        items = tuple(
            PanelItem(
                label=(f"#{session.position}" if session.position is not None else "  ")
                + f" {session.saved_at[:19]}",
                description=(
                    f"{session.model} · {session.preview[:40]} · {session.session_id}"
                    f"{' [active]' if session.active else ''}"
                ),
                action=ActionRequest(
                    "sessions.resume", ResumeSessionCommand(session.session_id)
                ),
                current=session.active,
            )
            for session in model.sessions
        ) or (
            PanelItem(
                label="(no saved sessions)",
                description="/save writes a restorable snapshot",
                action=None,
            ),
        )
        return PanelDefinition(
            view_type=model.view_type,
            title=title,
            items=items,
            filterable=True,
        )

    return CommandPanelSpec("sessions", SessionsViewModel, build)


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="sessions.list",
                preview=True,
                command_type=ListSessionsCommand,
                feature_id="sessions",
                description="[session-index] Browse saved sessions (add `all` to include every fingerprint)",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/session"),
                    slash_trigger("/session all"),
                    slash_trigger("/sessions"),
                    slash_trigger("/sessions all"),
                ),
                parser=_parse_list_sessions,
                handler=_handle_list_sessions,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="sessions.resume",
                command_type=ResumeSessionCommand,
                audit="session_lifecycle",
                feature_id="sessions",
                description="[session-index] Restore by displayed number, full ID, or newest fingerprint match",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/session <#|id|latest>"),),
                parser=_parse_resume_session,
                handler=_handle_resume_session,
            ),
            ActionSpec(
                action_id="sessions.save",
                command_type=SaveSessionCommand,
                audit="session_lifecycle",
                feature_id="sessions",
                description="[session] Save the current session with its runtime overrides and fingerprint",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/save"),),
                parser=_parse_save_session,
                handler=_handle_save_session,
            ),
            ActionSpec(
                action_id="sessions.new",
                command_type=NewSessionCommand,
                audit="session_lifecycle",
                feature_id="sessions",
                description="[session] Start a new session after auto-saving the current one",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/new"),),
                parser=_parse_new_session,
                handler=_handle_new_session,
            ),
        ]
    )
