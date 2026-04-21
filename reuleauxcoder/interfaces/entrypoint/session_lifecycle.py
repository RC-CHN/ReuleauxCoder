"""Session restore helpers for the shared app runner."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.app.runtime.session_state import (
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
) -> tuple[str | None, str | None, Path | None]:
    """Restore requested/latest session and return session runtime metadata."""
    current_session_id = None
    session_exit_time = None
    sessions_dir = Path(config.session_dir) if config.session_dir else None
    current_fingerprint = get_session_fingerprint(config, agent)

    session_store = dependencies.create_session_store(sessions_dir)
    if options.resume_session_id:
        loaded = session_store.load(options.resume_session_id)
        if loaded:
            if loaded.fingerprint != current_fingerprint:
                ui_bus.warning(
                    f"Session '{options.resume_session_id}' belongs to fingerprint '{loaded.fingerprint}', current fingerprint is '{current_fingerprint}'.",
                    kind=UIEventKind.SESSION,
                )
            apply_session_runtime_state(loaded, config, agent)
            setattr(agent, "session_fingerprint", loaded.fingerprint)
            current_session_id = options.resume_session_id
            setattr(agent, "current_session_id", current_session_id)
            session_exit_time = session_store.get_exit_time(loaded.messages)
            ui_bus.success(
                f"Resumed session: {options.resume_session_id}",
                kind=UIEventKind.SESSION,
            )
        else:
            ui_bus.error(
                f"Session '{options.resume_session_id}' not found.",
                kind=UIEventKind.SESSION,
            )
    elif options.auto_resume_latest:
        latest = session_store.get_latest(fingerprint=current_fingerprint)
        if latest:
            loaded = session_store.load(latest.id)
            if loaded:
                apply_session_runtime_state(loaded, config, agent)
                setattr(agent, "session_fingerprint", loaded.fingerprint)
                current_session_id = latest.id
                setattr(agent, "current_session_id", current_session_id)
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
    else:
        restore_config_runtime_defaults(config, agent)

    return current_session_id, session_exit_time, sessions_dir
