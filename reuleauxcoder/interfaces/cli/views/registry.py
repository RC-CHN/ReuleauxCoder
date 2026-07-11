"""CLI structured view renderer registry."""

from __future__ import annotations

from reuleauxcoder.interfaces.cli.views.builtin import builtin_cli_view_specs
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry


def create_cli_view_registry() -> ViewRendererRegistry:
    """Create the explicit CLI-owned structured view registry."""
    return ViewRendererRegistry(builtin_cli_view_specs())
