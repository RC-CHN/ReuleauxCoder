"""Session restore helpers for the shared app runner."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from reuleauxcoder.app.runtime.session_state import (
    bind_session_persistence,
    apply_session_runtime_state,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies, AppOptions
from reuleauxcoder.infrastructure.persistence.session_store import SessionRestoreError


_MAX_BUFFERED_RUNTIME_ISSUE_KEYS = 8
_MAX_RUNTIME_ISSUE_COUNT = 1_000_000
_RuntimeIssueSink = Callable[[str, str, str, int], None]


def _safe_callback_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name and len(name) <= 64 and name.isascii() and name.replace("_", "").isalnum():
        return name
    return "Exception"


def _safe_runtime_issue_field(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    if not value or len(value) > 64 or not value.isascii():
        return fallback
    if not value.replace("_", "").isalnum():
        return fallback
    return value


def _record_runtime_issue(
    agent: Agent,
    *,
    phase: str,
    error_type: str,
    ref: str,
    count: int = 1,
) -> bool:
    """Record a safe fact without letting the diagnostic path replace the result."""
    recorder = getattr(agent, "record_runtime_issue", None)
    if not callable(recorder):
        return False
    try:
        recorder(phase, error_type, ref, count)
    except KeyboardInterrupt:
        raise
    except BaseException:
        return False
    return True


def _record_observer_failure(
    agent: Agent,
    *,
    issue_sink: _RuntimeIssueSink | None,
    phase: str,
    ref: str,
    error: BaseException,
) -> None:
    error_type = _safe_callback_error_type(error)
    if issue_sink is None:
        _record_runtime_issue(
            agent,
            phase=phase,
            error_type=error_type,
            ref=ref,
        )
        return
    try:
        issue_sink(phase, error_type, ref, 1)
    except KeyboardInterrupt:
        raise
    except BaseException:
        pass


def observe_session_callback(
    callback,
    *args,
    agent: Agent,
    issue_sink: _RuntimeIssueSink | None = None,
    diagnostic_phase: str,
    diagnostic_ref: str,
    **kwargs,
) -> None:
    if not callable(callback):
        return
    try:
        callback(*args, **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        _record_observer_failure(
            agent,
            issue_sink=issue_sink,
            phase=diagnostic_phase,
            ref=diagnostic_ref,
            error=error,
        )


def _take_recovered_steering_discard_count(
    agent: Agent,
    *,
    issue_sink: _RuntimeIssueSink,
) -> int:
    """Read the optional recovery notice count without breaking old embeddings."""
    take_count = getattr(agent, "take_recovered_steering_discard_count", None)
    if not callable(take_count):
        return 0
    try:
        value = take_count()
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        _record_observer_failure(
            agent,
            issue_sink=issue_sink,
            phase="restore_observer",
            ref="steering_notice",
            error=error,
        )
        return 0
    return value if isinstance(value, int) else 0


def _report_restore_issues(
    loaded,
    agent: Agent,
    ui_bus: UIEventBus,
    progress: Callable[[str], None] | None,
    *,
    issue_sink: _RuntimeIssueSink,
) -> bool:
    """Expose optional-artifact degradation without blocking the restore."""
    issues = tuple(getattr(loaded, "restore_issues", ()))
    for issue in issues:
        observe_session_callback(
            ui_bus.warning,
            f"Session restored with degraded state ({issue.render()}).",
            kind=UIEventKind.SESSION,
            phase=issue.phase,
            error_type=issue.error_type,
            ref=issue.ref,
            count=issue.count,
            agent=agent,
            issue_sink=issue_sink,
            diagnostic_phase="restore_observer",
            diagnostic_ref="ui_bus",
        )
    if issues and progress is not None:
        observe_session_callback(
            progress,
            f"Session restore degraded ({len(issues)} issue(s)).",
            agent=agent,
            issue_sink=issue_sink,
            diagnostic_phase="restore_observer",
            diagnostic_ref="progress_callback",
        )
    return bool(issues)


def _report_inventory_issues(
    issues,
    agent: Agent,
    ui_bus: UIEventBus,
    progress: Callable[[str], None] | None,
    *,
    issue_sink: _RuntimeIssueSink,
) -> None:
    safe_issues = tuple(issues)
    agent.session_inventory_issues = safe_issues
    for issue in safe_issues:
        observe_session_callback(
            ui_bus.warning,
            f"Session inventory degraded ({issue.render()}).",
            kind=UIEventKind.SESSION,
            phase=issue.phase,
            error_type=issue.error_type,
            ref=issue.ref,
            count=issue.count,
            agent=agent,
            issue_sink=issue_sink,
            diagnostic_phase="inventory_observer",
            diagnostic_ref="ui_bus",
        )
    if safe_issues and progress is not None:
        observe_session_callback(
            progress,
            f"Session inventory degraded ({len(safe_issues)} issue(s)).",
            agent=agent,
            issue_sink=issue_sink,
            diagnostic_phase="inventory_observer",
            diagnostic_ref="progress_callback",
        )


def restore_session(
    options: AppOptions,
    dependencies: AppDependencies,
    config: Config,
    agent: Agent,
    ui_bus: UIEventBus,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[str | None, str | None, Path | None]:
    """Restore requested/latest session and return session runtime metadata."""
    current_session_id = None
    session_exit_time = None
    inventory_issues = ()
    inventory_reported = False
    sessions_dir = Path(config.session_dir) if config.session_dir else None
    current_fingerprint = get_session_fingerprint(config, agent)
    pending_runtime_issues: dict[tuple[str, str, str], int] = {}
    overflow_key = ("runtime_issue", "Overflow", "capacity")

    def buffer_runtime_issue(
        phase: str,
        error_type: str,
        ref: str,
        count: int = 1,
    ) -> None:
        key = (
            _safe_runtime_issue_field(phase, "runtime"),
            _safe_runtime_issue_field(error_type, "Exception"),
            _safe_runtime_issue_field(ref, "observer"),
        )
        safe_count = (
            min(count, _MAX_RUNTIME_ISSUE_COUNT)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0
            else 1
        )
        if key in pending_runtime_issues:
            pending_runtime_issues[key] = min(
                pending_runtime_issues[key] + safe_count,
                _MAX_RUNTIME_ISSUE_COUNT,
            )
        elif len(pending_runtime_issues) < _MAX_BUFFERED_RUNTIME_ISSUE_KEYS - 1:
            pending_runtime_issues[key] = safe_count
        else:
            pending_runtime_issues[overflow_key] = min(
                pending_runtime_issues.get(overflow_key, 0) + safe_count,
                _MAX_RUNTIME_ISSUE_COUNT,
            )

    def flush_runtime_issues() -> None:
        for (phase, error_type, ref), count in tuple(pending_runtime_issues.items()):
            if not _record_runtime_issue(
                agent,
                phase=phase,
                error_type=error_type,
                ref=ref,
                count=count,
            ):
                return
            pending_runtime_issues.pop((phase, error_type, ref), None)

    def report_progress(message: str) -> None:
        observe_session_callback(
            progress,
            message,
            agent=agent,
            issue_sink=buffer_runtime_issue,
            diagnostic_phase="restore_observer",
            diagnostic_ref="progress_callback",
        )

    def report_ui(callback, message: str, **metadata) -> None:
        observe_session_callback(
            callback,
            message,
            agent=agent,
            issue_sink=buffer_runtime_issue,
            diagnostic_phase="restore_observer",
            diagnostic_ref="ui_bus",
            **metadata,
        )

    session_store = dependencies.create_session_store(sessions_dir)
    set_progress = getattr(session_store, "set_progress_callback", None)
    if callable(set_progress):
        observe_session_callback(
            set_progress,
            report_progress if progress is not None else None,
            agent=agent,
            issue_sink=buffer_runtime_issue,
            diagnostic_phase="restore_observer",
            diagnostic_ref="progress_binding",
        )
    if options.resume_session_id:
        report_progress(f"Restoring requested session {options.resume_session_id}...")
        loaded = session_store.load(options.resume_session_id)
        if loaded:
            if loaded.fingerprint != current_fingerprint:
                report_ui(
                    ui_bus.warning,
                    f"Session '{options.resume_session_id}' belongs to fingerprint '{loaded.fingerprint}', current fingerprint is '{current_fingerprint}'.",
                    kind=UIEventKind.SESSION,
                )
            apply_session_runtime_state(loaded, config, agent)
            flush_runtime_issues()
            restore_degraded = _report_restore_issues(
                loaded,
                agent,
                ui_bus,
                progress,
                issue_sink=buffer_runtime_issue,
            )
            discarded_steering = _take_recovered_steering_discard_count(
                agent,
                issue_sink=buffer_runtime_issue,
            )
            if discarded_steering:
                report_ui(
                    ui_bus.warning,
                    f"{discarded_steering} queued steering message(s) from the "
                    "interrupted session were not sent and have been discarded.",
                    kind=UIEventKind.SESSION,
                )
            agent.session_fingerprint = loaded.fingerprint
            current_session_id = options.resume_session_id
            agent.current_session_id = current_session_id
            session_exit_time = session_store.get_exit_time(loaded.messages)
            restored_notice = ui_bus.warning if restore_degraded else ui_bus.success
            report_ui(
                restored_notice,
                (
                    "Resumed session with degraded recovery: "
                    f"{options.resume_session_id}"
                    if restore_degraded
                    else f"Resumed session: {options.resume_session_id}"
                ),
                kind=UIEventKind.SESSION,
            )
            report_progress(
                f"Restored {len(loaded.messages)} message(s) and "
                f"{len(loaded.history_events)} history event(s)."
            )
        else:
            report_ui(
                ui_bus.error,
                f"Session '{options.resume_session_id}' not found.",
                kind=UIEventKind.SESSION,
            )
            raise SessionRestoreError(
                phase="session_discovery",
                error_type="FileNotFoundError",
                ref="session",
            ) from None
    elif options.auto_resume_latest:
        report_progress("Looking for the latest compatible session...")
        latest_result = session_store.get_latest_result(fingerprint=current_fingerprint)
        latest = latest_result.session
        inventory_issues = tuple(latest_result.issues)
        if latest:
            report_progress(f"Restoring latest session {latest.id}...")
            loaded = session_store.load(latest.id)
            if loaded:
                apply_session_runtime_state(loaded, config, agent)
                flush_runtime_issues()
                _report_inventory_issues(
                    inventory_issues,
                    agent,
                    ui_bus,
                    progress,
                    issue_sink=buffer_runtime_issue,
                )
                inventory_reported = True
                restore_degraded = _report_restore_issues(
                    loaded,
                    agent,
                    ui_bus,
                    progress,
                    issue_sink=buffer_runtime_issue,
                )
                discarded_steering = _take_recovered_steering_discard_count(
                    agent,
                    issue_sink=buffer_runtime_issue,
                )
                if discarded_steering:
                    report_ui(
                        ui_bus.warning,
                        f"{discarded_steering} queued steering message(s) from the "
                        "interrupted session were not sent and have been discarded.",
                        kind=UIEventKind.SESSION,
                    )
                agent.session_fingerprint = loaded.fingerprint
                current_session_id = latest.id
                agent.current_session_id = current_session_id
                session_exit_time = session_store.get_exit_time(loaded.messages)
                restored_notice = ui_bus.warning if restore_degraded else ui_bus.info
                report_ui(
                    restored_notice,
                    (
                        "Auto-resumed latest session with degraded recovery: "
                        f"{latest.id} ({latest.saved_at})"
                        if restore_degraded
                        else (
                            f"Auto-resumed latest session: {latest.id} "
                            f"({latest.saved_at})"
                        )
                    ),
                    kind=UIEventKind.SESSION,
                )
                if latest.preview:
                    report_ui(
                        ui_bus.info,
                        f"  Preview: {latest.preview}...",
                        kind=UIEventKind.SESSION,
                    )
                report_progress(
                    f"Restored {len(loaded.messages)} message(s) and "
                    f"{len(loaded.history_events)} history event(s)."
                )
            else:
                raise SessionRestoreError(
                    phase="session_load",
                    error_type="FileNotFoundError",
                    ref="session",
                ) from None
        else:
            report_progress(
                "No compatible saved session found; starting a new session."
            )
    else:
        report_progress("Session restore disabled; starting a new session.")

    if current_session_id is None:
        restore_config_runtime_defaults(config, agent)
        flush_runtime_issues()
        if not inventory_reported:
            _report_inventory_issues(
                inventory_issues,
                agent,
                ui_bus,
                progress,
                issue_sink=buffer_runtime_issue,
            )
        current_session_id = session_store.generate_session_id()
        agent.current_session_id = current_session_id
    bind_session_persistence(
        config,
        agent,
        session_store,
        current_session_id,
        fingerprint=getattr(agent, "session_fingerprint", None) or current_fingerprint,
    )
    flush_runtime_issues()

    return current_session_id, session_exit_time, sessions_dir
