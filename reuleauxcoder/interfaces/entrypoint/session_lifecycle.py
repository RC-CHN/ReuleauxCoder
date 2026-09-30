"""Session restore helpers for the shared app runner."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path

from reuleauxcoder.app.runtime.session_state import (
    bind_session_persistence,
    apply_session_runtime_state,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.session.models import SessionRestoreIssue
from reuleauxcoder.app.ui_events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies, AppOptions
from reuleauxcoder.infrastructure.persistence.session_store import SessionRestoreError


_logger = logging.getLogger(__name__)


def observe_session_callback(
    callback,
    *args,
    diagnostic_phase: str,
    diagnostic_ref: str,
    **kwargs,
):
    """Invoke an optional callback, logging unexpected failures before propagation."""
    if callback is None:
        return None
    try:
        return callback(*args, **kwargs)
    except KeyboardInterrupt:
        raise
    except BaseException:
        _logger.exception("Callback failed: %s/%s", diagnostic_phase, diagnostic_ref)
        raise


def _take_recovered_steering_discard_count(agent: Agent) -> int:
    """Read the optional recovery notice count."""
    value = observe_session_callback(
        getattr(agent, "take_recovered_steering_discard_count", None),
        diagnostic_phase="restore_observer",
        diagnostic_ref="steering_notice",
    )
    return value if isinstance(value, int) else 0


def _report_restore_issues(
    loaded,
    ui_bus: UIEventBus,
    progress: Callable[[str], None] | None,
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
            diagnostic_phase="restore_observer",
            diagnostic_ref="ui_bus",
        )
    if issues and progress is not None:
        observe_session_callback(
            progress,
            f"Session restore degraded ({len(issues)} issue(s)).",
            diagnostic_phase="restore_observer",
            diagnostic_ref="progress_callback",
        )
    return bool(issues)


def _report_inventory_issues(
    issues,
    agent: Agent,
    ui_bus: UIEventBus,
    progress: Callable[[str], None] | None,
) -> None:
    safe_issues = tuple(issues)
    agent.session_inventory_issues = safe_issues
    for issue in safe_issues:
        observe_session_callback(
            ui_bus.warning,
            "Some saved session data could not be read. Unreadable data was "
            f"skipped; original files were kept. ({issue.render()})",
            kind=UIEventKind.SESSION,
            phase=issue.phase,
            error_type=issue.error_type,
            ref=issue.ref,
            count=issue.count,
            diagnostic_phase="inventory_observer",
            diagnostic_ref="ui_bus",
        )
    if safe_issues and progress is not None:
        observe_session_callback(
            progress,
            f"Session inventory degraded ({len(safe_issues)} issue(s)).",
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

    def report_progress(message: str) -> None:
        observe_session_callback(
            progress,
            message,
            diagnostic_phase="restore_observer",
            diagnostic_ref="progress_callback",
        )

    def report_ui(callback, message: str, **metadata) -> None:
        observe_session_callback(
            callback,
            message,
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
            restore_degraded = _report_restore_issues(
                loaded,
                ui_bus,
                progress,
            )
            discarded_steering = _take_recovered_steering_discard_count(agent)
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
        # Automatic startup can use readable entries while explicit resume
        # continues to require the requested snapshot to validate completely.
        inventory = session_store.list_result(limit=1, fingerprint=current_fingerprint)
        latest = next(iter(inventory.sessions), None)
        inventory_issues = tuple(inventory.issues)
        if latest:
            report_progress(f"Restoring latest session {latest.id}...")
            try:
                loaded = session_store.load(latest.id)
                if loaded is None:
                    raise SessionRestoreError(
                        phase="session_load",
                        error_type="FileNotFoundError",
                        ref="session",
                    )
            except SessionRestoreError as error:
                loaded = None
                issue = SessionRestoreIssue(
                    phase=error.phase, error_type=error.error_type, ref=error.ref,
                )
                report_ui(
                    ui_bus.warning,
                    "Could not restore the previous workspace session. Starting a "
                    f"new session; original files were kept. ({issue.render()})",
                    kind=UIEventKind.SESSION,
                    phase=issue.phase,
                    error_type=issue.error_type,
                    ref=issue.ref,
                )
            if loaded:
                apply_session_runtime_state(loaded, config, agent)
                _report_inventory_issues(
                    inventory_issues,
                    agent,
                    ui_bus,
                    progress,
                )
                inventory_reported = True
                restore_degraded = _report_restore_issues(
                    loaded,
                    ui_bus,
                    progress,
                )
                discarded_steering = _take_recovered_steering_discard_count(agent)
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
            report_progress(
                "No compatible saved session found; starting a new session."
            )
    else:
        report_progress("Session restore disabled; starting a new session.")

    if current_session_id is None:
        restore_config_runtime_defaults(config, agent)
        if not inventory_reported:
            _report_inventory_issues(
                inventory_issues,
                agent,
                ui_bus,
                progress,
            )
        current_session_id = session_store.generate_session_id()
        agent.current_session_id = current_session_id
    bind_issue = bind_session_persistence(
        config,
        agent,
        session_store,
        current_session_id,
        fingerprint=getattr(agent, "session_fingerprint", None) or current_fingerprint,
    )
    if bind_issue is not None:
        report_ui(
            ui_bus.warning,
            "Session is active, but persistence is unavailable "
            f"({bind_issue.render()}).",
            kind=UIEventKind.SESSION,
            phase=bind_issue.phase,
            error_type=bind_issue.error_type,
            ref=bind_issue.ref,
        )
        report_progress("Session persistence is unavailable; model requests are paused.")

    return current_session_id, session_exit_time, sessions_dir
