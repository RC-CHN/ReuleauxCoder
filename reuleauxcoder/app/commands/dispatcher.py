"""Shared command dispatcher."""

from __future__ import annotations

from reuleauxcoder.app.commands.handlers.model import handle_show_model, handle_switch_model
from reuleauxcoder.app.commands.models import (
    Command,
    CommandContext,
    CommandResult,
    ShowModelCommand,
    SwitchModelCommand,
)


def dispatch_command(command: Command, ctx: CommandContext) -> CommandResult:
    """Dispatch a structured command to its shared handler."""
    if isinstance(command, ShowModelCommand):
        return handle_show_model(ctx)
    if isinstance(command, SwitchModelCommand):
        return handle_switch_model(command, ctx)
    return CommandResult(action="continue")
