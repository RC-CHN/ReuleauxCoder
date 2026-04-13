"""Decorator-based command module registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from reuleauxcoder.app.commands.registry import ActionRegistry

CommandModuleRegistrar = Callable[[ActionRegistry], None]

_REGISTRARS: list[CommandModuleRegistrar] = []


def register_command_module(func: CommandModuleRegistrar) -> CommandModuleRegistrar:
    """Register a command module action registrar function."""
    if func not in _REGISTRARS:
        _REGISTRARS.append(func)
    return func


def iter_command_module_registrars() -> Iterator[CommandModuleRegistrar]:
    """Iterate registered command module registrar functions."""
    return iter(_REGISTRARS)
