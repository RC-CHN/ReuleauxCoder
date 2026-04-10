"""CLI command handlers."""

from pathlib import Path

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.interfaces.events import UIEventBus


def handle_command(
    user_input: str,
    agent,
    config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    sessions_dir: Path | None = None,
):
    parsed_command = parse_command(user_input, current_session_id=current_session_id)
    if parsed_command is not None:
        result = dispatch_command(
            parsed_command,
            CommandContext(
                agent=agent,
                config=config,
                ui_bus=ui_bus,
                ui_interactor=getattr(agent, "ui_interactor", None),
                sessions_dir=sessions_dir,
            ),
        )
        return {
            "action": result.action,
            "session_id": result.session_id if result.session_id is not None else current_session_id,
            "session_exit_time": result.session_exit_time,
        }

    return {"action": "chat", "session_id": current_session_id}
