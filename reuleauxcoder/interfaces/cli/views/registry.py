"""CLI structured view renderer registry."""

from __future__ import annotations

from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.interfaces.view_registration import build_view_specs
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry


def create_cli_view_registry() -> ViewRendererRegistry:
    """Create the CLI view registry from decorator-registered view renderers."""
    create_builtin_action_registry()
    return ViewRendererRegistry(build_view_specs(ui_target="cli"))
