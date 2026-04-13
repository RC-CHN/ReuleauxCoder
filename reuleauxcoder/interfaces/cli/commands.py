"""CLI command handlers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.interfaces.events import UIEventBus
from reuleauxcoder.interfaces.ui_registry import UIProfile

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.skills.service import SkillsService


def handle_command(
    user_input: str,
    agent: Agent,
    config: Config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
    sessions_dir: Path | None = None,
    skills_service: SkillsService | None = None,
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
                action_registry=parsed_action.registry,
                ui_interactor=getattr(agent, "ui_interactor", None),
                sessions_dir=sessions_dir,
                skills_service=skills_service,
            ),
        )
        return {
            "action": result.action,
            "session_id": result.session_id if result.session_id is not None else current_session_id,
            "session_exit_time": result.session_exit_time,
        }

    return {"action": "chat", "session_id": current_session_id}

