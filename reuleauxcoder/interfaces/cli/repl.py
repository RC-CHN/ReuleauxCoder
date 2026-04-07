"""Interactive REPL loop."""

from pathlib import Path

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory

from reuleauxcoder import __version__
from reuleauxcoder.infrastructure.fs.paths import ensure_user_dirs, get_history_file
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.render import show_banner
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.services.sessions.manager import save_session


def run_repl(
    agent,
    config,
    ui_bus: UIEventBus,
    current_session_id: str = None,
    sessions_dir: Path | None = None,
) -> None:
    ensure_user_dirs()
    show_banner(config.model, config.base_url, __version__)

    hist_path = str(get_history_file())
    history = FileHistory(hist_path)

    while True:
        try:
            user_input = pt_prompt("You > ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            ui_bus.info("\nBye!")
            if agent.messages:
                sid = save_session(agent.messages, config.model, current_session_id, sessions_dir)
                ui_bus.info(f"Session auto-saved: {sid}", kind=UIEventKind.SESSION)
            break

        if not user_input:
            continue

        result = handle_command(
            user_input, agent, config, current_session_id, ui_bus, sessions_dir
        )
        current_session_id = result["session_id"]
        if result["action"] == "exit":
            break
        if result["action"] == "continue":
            continue

        try:
            agent.chat(user_input)
        except KeyboardInterrupt:
            ui_bus.warning("Interrupted.")
        except Exception as e:
            ui_bus.error(f"Error: {e}")