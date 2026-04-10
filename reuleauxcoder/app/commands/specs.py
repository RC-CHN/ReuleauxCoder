"""Declarative action/trigger specs for shared command execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from reuleauxcoder.app.commands.models import CommandContext, CommandResult
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


class TriggerKind(str, Enum):
    """Supported action trigger kinds."""

    SLASH = "slash"
    PALETTE = "palette"
    BUTTON = "button"
    MENU = "menu"
    SHORTCUT = "shortcut"


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """One way to invoke an action."""

    kind: TriggerKind
    value: str
    ui_targets: frozenset[str] = field(default_factory=frozenset)
    required_capabilities: frozenset[UICapability] = field(default_factory=frozenset)

    def is_available_in(
        self,
        ui_profile: UIProfile,
        *,
        fallback_ui_targets: frozenset[str] | None = None,
    ) -> bool:
        """Return whether this trigger is available in the given UI profile."""
        targets = self.ui_targets or (fallback_ui_targets or frozenset())
        if targets and ui_profile.ui_id not in targets:
            return False
        return self.required_capabilities.issubset(ui_profile.capabilities)


@dataclass(frozen=True, slots=True)
class CommandParseContext:
    """Parsing context for command-like triggers."""

    current_session_id: str | None
    ui_profile: UIProfile


ActionParser = Callable[[str, CommandParseContext], object | None]
ActionHandler = Callable[[object, CommandContext], CommandResult]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Declarative action definition."""

    action_id: str
    feature_id: str
    description: str
    ui_targets: frozenset[str]
    required_capabilities: frozenset[UICapability] = field(default_factory=frozenset)
    triggers: tuple[TriggerSpec, ...] = ()
    parser: ActionParser | None = None
    handler: ActionHandler | None = None
    interactive: bool = False

    def is_available_in(self, ui_profile: UIProfile) -> bool:
        """Return whether this action is available in the given UI profile."""
        if ui_profile.ui_id not in self.ui_targets:
            return False
        return self.required_capabilities.issubset(ui_profile.capabilities)

    def matching_triggers(self, ui_profile: UIProfile, *, kind: TriggerKind | None = None) -> tuple[TriggerSpec, ...]:
        """Return triggers available for a UI profile, optionally filtered by trigger kind."""
        matched: list[TriggerSpec] = []
        for trigger in self.triggers:
            if kind is not None and trigger.kind != kind:
                continue
            if trigger.is_available_in(ui_profile, fallback_ui_targets=self.ui_targets):
                matched.append(trigger)
        return tuple(matched)
