"""Interactive REPL loop."""

import time
from pathlib import Path

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory

from reuleauxcoder import __version__
from reuleauxcoder.infrastructure.fs.paths import ensure_user_dirs, get_history_file
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.render import show_banner
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.ui_registry import UIProfile


def run_repl(
    agent,
    config,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
    current_session_id: str = None,
    sessions_dir: Path | None = None,
    session_exit_time: str | None = None,
    skills_service=None,
) -> None:
    ensure_user_dirs()
    show_banner(config.model, config.base_url, __version__)

    hist_path = str(get_history_file())
    history = FileHistory(hist_path)

    pending_resume_prefix: str | None = None
    if session_exit_time is not None:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        pending_resume_prefix = (
            f"[SESSION_RESUME] User returned to the session at {current_time} "
            f"(last left at {session_exit_time}).\n\n"
        )

    while True:
        try:
            user_input = pt_prompt("You > ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            ui_bus.info("\nBye!")
            if agent.messages:
                sid = SessionStore(sessions_dir).save(
                    agent.messages,
                    config.model,
                    current_session_id,
                    is_exit=True,
                    total_prompt_tokens=agent.state.total_prompt_tokens,
                    total_completion_tokens=agent.state.total_completion_tokens,
                    active_mode=getattr(agent, "active_mode", None),
                )
                ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION)
            break

        if not user_input:
            continue

        result = handle_command(
            user_input,
            agent,
            config,
            current_session_id,
            ui_bus,
            ui_profile,
            sessions_dir,
            skills_service,
        )
        prev_session_id = current_session_id
        current_session_id = result["session_id"]

        resumed_exit_time = result.get("session_exit_time")
        if resumed_exit_time is not None:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            pending_resume_prefix = (
                f"[SESSION_RESUME] User returned to the session at {current_time} "
                f"(last left at {resumed_exit_time}).\n\n"
            )
        elif result["action"] == "continue" and current_session_id != prev_session_id:
            # Session switched/reset (e.g. /new, /session): stale resume marker should not leak.
            pending_resume_prefix = None

        if result["action"] == "exit":
            break
        if result["action"] == "continue":
            continue

        chat_input = user_input
        if pending_resume_prefix is not None:
            chat_input = pending_resume_prefix + chat_input
            pending_resume_prefix = None

        try:
            agent.chat(chat_input)
        except KeyboardInterrupt:
            ui_bus.warning("Interrupted.")
        except Exception as e:
            ui_bus.error(f"Error: {e}")
