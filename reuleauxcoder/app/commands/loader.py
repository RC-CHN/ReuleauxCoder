"""Builtin command extension loader and defaults."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from reuleauxcoder.app.commands.registry import ActionRegistry

BUILTIN_COMMAND_EXTENSION_MODULES = (
    "reuleauxcoder.extensions.command.builtin.system",
    "reuleauxcoder.extensions.command.builtin.model",
    "reuleauxcoder.extensions.command.builtin.sessions",
    "reuleauxcoder.extensions.command.builtin.approval",
    "reuleauxcoder.extensions.command.builtin.mcp",
)


def _load_module(path: str) -> ModuleType:
    return import_module(path)


def create_builtin_action_registry() -> ActionRegistry:
    """Create and populate action registry from builtin command extension modules."""
    registry = ActionRegistry()
    for module_path in BUILTIN_COMMAND_EXTENSION_MODULES:
        module = _load_module(module_path)
        register = getattr(module, "register_actions", None)
        if callable(register):
            register(registry)
    return registry
