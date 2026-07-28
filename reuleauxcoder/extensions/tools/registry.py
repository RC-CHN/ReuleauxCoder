"""Explicit builtin tool catalog and builders."""

from __future__ import annotations

from typing import Optional

from reuleauxcoder.extensions.tools.builtin import builtin_tool_types
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool


def iter_tool_classes() -> tuple[type[Tool], ...]:
    """Return builtin tool classes in stable schema order."""
    return builtin_tool_types()


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


# ALL_TOOLS removed — use build_tools(backend) for explicit instantiation.
# Previously this was a module-level singleton built eagerly at import time.
