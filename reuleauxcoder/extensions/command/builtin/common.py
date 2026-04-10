"""Action spec helpers for builtin command extensions."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.specs import TriggerKind, TriggerSpec
from reuleauxcoder.interfaces.ui_registry import UICapability

UI_TARGETS = frozenset({"cli", "tui", "vscode"})
TEXT_REQUIRED = frozenset({UICapability.TEXT_INPUT})


@dataclass(frozen=True, slots=True)
class EmptyCommand:
    """Marker command object for actions with no parse payload."""


def slash_trigger(value: str) -> TriggerSpec:
    """Build a CLI slash trigger declaration with text-input capability requirement."""
    return TriggerSpec(
        kind=TriggerKind.SLASH,
        value=value,
        ui_targets=frozenset({"cli"}),
        required_capabilities=TEXT_REQUIRED,
    )
