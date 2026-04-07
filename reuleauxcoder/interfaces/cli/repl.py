"""Interactive REPL loop."""

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory

from reuleauxcoder import __version__
from reuleauxcoder.infrastructure.fs.paths import ensure_user_dirs, get_history_file
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.render import (
    console,
    show_banner,
    CLIRenderer,
)
from reuleauxcoder.services.sessions.manager import save_session


def run_repl(agent, config) -> None:
    ensure_user_dirs()
    show_banner(config.model, config.base_url, __version__)

    hist_path = str(get_history_file())
    history = FileHistory(hist_path)
    current_session_id = None

    # Create event-driven renderer and subscribe to agent events
    renderer = CLIRenderer()
    agent.add_event_handler(renderer.on_event)

    while True:
        try:
            user_input = pt_prompt("You > ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            if agent.messages:
                sid = save_session(agent.messages, config.model, current_session_id)
                console.print(f"[dim]Session auto-saved: {sid}[/dim]")
            break

        if not user_input:
            continue

        result = handle_command(user_input, agent, config, current_session_id)
        current_session_id = result["session_id"]
        if result["action"] == "exit":
            break
        if result["action"] == "continue":
            continue

        try:
            agent.chat(user_input)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")