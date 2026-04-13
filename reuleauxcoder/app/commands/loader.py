"""Builtin command extension loader and defaults."""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules

from reuleauxcoder.app.commands.module_registry import iter_command_module_registrars
from reuleauxcoder.app.commands.registry import ActionRegistry

_BUILTIN_COMMAND_PACKAGE = "reuleauxcoder.extensions.command.builtin"


def _import_builtin_command_modules() -> None:
    """Import all builtin command extension modules so decorators can register them."""
    package = import_module(_BUILTIN_COMMAND_PACKAGE)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return

    for module_info in iter_modules(package_paths):
        if module_info.name.startswith("_"):
            continue
        import_module(f"{_BUILTIN_COMMAND_PACKAGE}.{module_info.name}")


def create_builtin_action_registry() -> ActionRegistry:
    """Create and populate action registry from builtin command extension modules."""
    registry = ActionRegistry()
    _import_builtin_command_modules()
    for register in iter_command_module_registrars():
        register(registry)
    return registry
