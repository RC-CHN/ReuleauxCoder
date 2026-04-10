"""Parser for shared slash commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import Command, ShowModelCommand, SwitchModelCommand


def parse_command(user_input: str) -> Command | None:
    """Parse a slash command into a structured command object.

    For now this parser only owns the `/model` command family.
    """
    if user_input == "/model":
        return ShowModelCommand()

    if user_input.startswith("/model "):
        target = user_input[7:].strip()
        if target in {"", "ls", "list", "show"}:
            return ShowModelCommand()
        return SwitchModelCommand(profile_name=target)

    return None
