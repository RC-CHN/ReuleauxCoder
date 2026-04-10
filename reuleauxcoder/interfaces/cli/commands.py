"""CLI command handlers."""

from pathlib import Path

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.app.commands.actions import ACTION_REGISTRY
from reuleauxcoder.interfaces.events import UIEventBus
from reuleauxcoder.interfaces.ui_registry import UIProfile


def handle_command(
    user_input: str,
    agent,
    config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
    sessions_dir: Path | None = None,
):
    parsed_action = parse_command(
        user_input,
        ui_profile=ui_profile,
        current_session_id=current_session_id,
    )
    if parsed_action is not None:
        result = dispatch_command(
            parsed_action,
            CommandContext(
                agent=agent,
                config=config,
                ui_bus=ui_bus,
                ui_profile=ui_profile,
                action_registry=ACTION_REGISTRY,
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

