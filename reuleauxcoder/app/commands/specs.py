"""Declarative action/trigger specs for shared command execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal

from reuleauxcoder.app.commands.models import CommandContext, CommandEffect
from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile


class TriggerKind(str, Enum):
    """Supported action trigger kinds."""

    SLASH = "slash"
    PALETTE = "palette"
    BUTTON = "button"
    MENU = "menu"
    SHORTCUT = "shortcut"


class DuringTurnPolicy(str, Enum):
    """How a slash command behaves while an agent turn is still running."""

    IMMEDIATE = "immediate"
    DEFER_UNTIL_IDLE = "defer_until_idle"


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

    ui_profile: UIProfile


ActionParser = Callable[[str, CommandParseContext], object | None]
ActionHandler = Callable[[object, CommandContext], CommandEffect]


@dataclass(frozen=True, slots=True)
class ActionParameter:
    """Form fields derived from the registered command's parameter dataclass."""

    name: str
    kind: str
    required: bool
    nullable: bool = False
    default: str | int | bool | None = None


@dataclass(frozen=True, slots=True)
class ActionDescription:
    """Read-only frontend description, containing no parsers or handlers."""

    action_id: str
    feature_id: str
    description: str
    ui_targets: frozenset[str]
    required_capabilities: frozenset[UICapability] = field(default_factory=frozenset)
    triggers: tuple[TriggerSpec, ...] = ()
    interactive: bool = False
    during_turn: DuringTurnPolicy = DuringTurnPolicy.DEFER_UNTIL_IDLE
    preview: bool = False
    parameters: tuple[ActionParameter, ...] = ()

    def is_available_in(self, ui_profile: UIProfile) -> bool:
        """Return whether this action is available in the given UI profile."""
        if self.ui_targets and ui_profile.ui_id not in self.ui_targets:
            return False
        return self.required_capabilities.issubset(ui_profile.capabilities)

    def matching_triggers(
        self, ui_profile: UIProfile, *, kind: TriggerKind | None = None
    ) -> tuple[TriggerSpec, ...]:
        """Return triggers available for a UI profile, optionally filtered by trigger kind."""
        matched: list[TriggerSpec] = []
        for trigger in self.triggers:
            if kind is not None and trigger.kind != kind:
                continue
            if trigger.is_available_in(ui_profile, fallback_ui_targets=self.ui_targets):
                matched.append(trigger)
        return tuple(matched)


@dataclass(frozen=True, slots=True)
class ActionSpec(ActionDescription):
    parser: ActionParser | None = None
    handler: ActionHandler | None = None
    command_type: type = object
    audit: Literal["runtime_config_changed", "session_lifecycle"] | None = None


@dataclass(frozen=True, slots=True)
class ActionCatalog:
    actions: tuple[ActionDescription, ...]

    def iter_actions(self, ui_profile: UIProfile) -> tuple[ActionDescription, ...]:
        return tuple(
            action for action in self.actions if action.is_available_in(ui_profile)
        )
