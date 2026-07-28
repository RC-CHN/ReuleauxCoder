"""Builtin command extension loader and defaults."""

from __future__ import annotations

from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.extensions.command.builtin import builtin_command_registrars


def create_builtin_action_registry() -> ActionRegistry:
    """Create and populate the explicit builtin action registry."""
    registry = ActionRegistry()
    for register in builtin_command_registrars():
        register(registry)
    return registry
