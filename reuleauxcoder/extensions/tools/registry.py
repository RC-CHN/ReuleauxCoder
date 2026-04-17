"""Decorator-based tool registry and builders."""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules
from typing import Optional

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool

_BUILTIN_TOOL_PACKAGE = "reuleauxcoder.extensions.tools.builtin"
_TOOL_CLASSES: list[type[Tool]] = []


def register_tool(cls: type[Tool]) -> type[Tool]:
    """Register a tool class for builder-based instantiation."""
    if cls not in _TOOL_CLASSES:
        _TOOL_CLASSES.append(cls)
    return cls


def _import_builtin_tool_modules() -> None:
    """Import builtin tool modules so decorator registrations run."""
    package = import_module(_BUILTIN_TOOL_PACKAGE)
    package_paths = getattr(package, "__path__", None)
    if package_paths is None:
        return

    for module_info in iter_modules(package_paths):
        if module_info.name.startswith("_"):
            continue
        import_module(f"{_BUILTIN_TOOL_PACKAGE}.{module_info.name}")


def iter_tool_classes() -> tuple[type[Tool], ...]:
    """Return registered tool classes."""
    _import_builtin_tool_modules()
    return tuple(_TOOL_CLASSES)


def build_tools(backend: ToolBackend | None = None) -> list[Tool]:
    """Instantiate all registered tool classes with the provided backend."""
    effective_backend = backend or LocalToolBackend()
    return [tool_cls(backend=effective_backend) for tool_cls in iter_tool_classes()]


def get_tool(name: str, backend: ToolBackend | None = None) -> Optional[Tool]:
    """Instantiate a tool by name."""
    for tool_cls in iter_tool_classes():
        if tool_cls.name == name:
            return tool_cls(backend=backend or LocalToolBackend())
    return None


ALL_TOOLS = build_tools()
