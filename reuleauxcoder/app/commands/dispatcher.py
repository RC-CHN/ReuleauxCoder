"""Shared action dispatcher."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import CommandContext, CommandResult
from reuleauxcoder.app.commands.registry import ParsedAction


def dispatch_command(parsed: ParsedAction, ctx: CommandContext) -> CommandResult:
    """Dispatch a parsed action through the source action registry."""
    return parsed.registry.dispatch(parsed, ctx)

