"""Explicit builtin tool catalog and builders."""

from __future__ import annotations

from reuleauxcoder.extensions.tools.builtin import builtin_tool_types
from reuleauxcoder.extensions.tools.backend import ToolBackend
from reuleauxcoder.extensions.tools.base import Tool


def iter_tool_classes() -> tuple[type[Tool], ...]:
    """Return builtin tool classes in stable schema order."""
    return builtin_tool_types()


def build_tools(backend: ToolBackend) -> list[Tool]:
    """Instantiate all registered tool classes with the provided backend."""
    return [tool_cls(backend=backend) for tool_cls in iter_tool_classes()]


# ALL_TOOLS removed — use build_tools(backend) for explicit instantiation.
# Previously this was a module-level singleton built eagerly at import time.
