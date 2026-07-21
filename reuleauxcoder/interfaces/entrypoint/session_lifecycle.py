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
    sessions_dir = Path(config.session_dir) if config.session_dir else None
    current_fingerprint = get_session_fingerprint(config, agent)

    session_store = dependencies.create_session_store(sessions_dir)
    set_progress = getattr(session_store, "set_progress_callback", None)
    if callable(set_progress):
        set_progress(progress)
    if options.resume_session_id:
        if progress is not None:
            progress(f"Restoring requested session {options.resume_session_id}...")
        loaded = session_store.load(options.resume_session_id)
        if loaded:
            if loaded.fingerprint != current_fingerprint:
                ui_bus.warning(
                    f"Session '{options.resume_session_id}' belongs to fingerprint '{loaded.fingerprint}', current fingerprint is '{current_fingerprint}'.",
                    kind=UIEventKind.SESSION,
                )
            apply_session_runtime_state(loaded, config, agent)
            agent.session_fingerprint = loaded.fingerprint
            current_session_id = options.resume_session_id
            agent.current_session_id = current_session_id
            session_exit_time = session_store.get_exit_time(loaded.messages)
            ui_bus.success(
                f"Resumed session: {options.resume_session_id}",
                kind=UIEventKind.SESSION,
            )
            if progress is not None:
                progress(
                    f"Restored {len(loaded.messages)} message(s) and "
                    f"{len(loaded.history_events)} history event(s)."
                )
        else:
            ui_bus.error(
                f"Session '{options.resume_session_id}' not found.",
                kind=UIEventKind.SESSION,
            )
            if progress is not None:
                progress("Requested session was not found; starting a new session.")
    elif options.auto_resume_latest:
        if progress is not None:
            progress("Looking for the latest compatible session...")
        latest = session_store.get_latest(fingerprint=current_fingerprint)
        if latest:
            if progress is not None:
                progress(f"Restoring latest session {latest.id}...")
            loaded = session_store.load(latest.id)
            if loaded:
                apply_session_runtime_state(loaded, config, agent)
                agent.session_fingerprint = loaded.fingerprint
                current_session_id = latest.id
                agent.current_session_id = current_session_id
                session_exit_time = session_store.get_exit_time(loaded.messages)
                ui_bus.info(
                    f"Auto-resumed latest session: {latest.id} ({latest.saved_at})",
                    kind=UIEventKind.SESSION,
                )
                if latest.preview:
                    ui_bus.info(
                        f"  Preview: {latest.preview}...",
                        kind=UIEventKind.SESSION,
                    )
                if progress is not None:
                    progress(
                        f"Restored {len(loaded.messages)} message(s) and "
                        f"{len(loaded.history_events)} history event(s)."
                    )
            elif progress is not None:
                progress("Latest session could not be restored; starting a new session.")
        elif progress is not None:
            progress("No compatible saved session found; starting a new session.")
    else:
        if progress is not None:
            progress("Session restore disabled; starting a new session.")
        restore_config_runtime_defaults(config, agent)

    if current_session_id is None:
        current_session_id = session_store.generate_session_id()
        agent.current_session_id = current_session_id
    bind_session_persistence(
        config,
        agent,
        session_store,
        current_session_id,
        fingerprint=getattr(agent, "session_fingerprint", None) or current_fingerprint,
    )

    return current_session_id, session_exit_time, sessions_dir
