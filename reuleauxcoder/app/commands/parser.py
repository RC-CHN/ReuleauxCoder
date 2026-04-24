"""Parser for shared actions via declarative action specs."""

from __future__ import annotations

from reuleauxcoder.app.commands.actions import ACTION_REGISTRY
from reuleauxcoder.app.commands.registry import ParsedAction
from reuleauxcoder.interfaces.ui_registry import UIProfile


def parse_command(
    user_input: str,
    *,
    ui_profile: UIProfile,
    current_session_id: str | None = None,
) -> ParsedAction | None:
    """Parse user input through the action registry for the current UI profile."""
    return ACTION_REGISTRY.parse(
        user_input,
        ui_profile=ui_profile,
        current_session_id=current_session_id,
    )
