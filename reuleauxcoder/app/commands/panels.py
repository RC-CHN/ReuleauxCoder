"""UI-neutral command-owned selection panel contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


@dataclass(frozen=True, slots=True)
class PanelItem:
    """One semantic panel row with an optional canonical command."""

    label: str
    description: str
    command: str
    current: bool = False


@dataclass(frozen=True, slots=True)
class PanelDefinition:
    """Immutable panel tree produced by a command feature."""

    view_type: str
    title: str
    items: tuple[PanelItem, ...]
    children: tuple[tuple[str, "PanelDefinition"], ...] = ()
    filterable: bool = False
    keep_open_on_submit: bool = False
    return_to_parent_on_submit: bool = False

    def child_for(self, label: str) -> "PanelDefinition | None":
        """Return the child panel attached to a row label."""
        return next(
            (child for item_label, child in self.children if item_label == label),
            None,
        )


class PanelRefreshPolicy(str, Enum):
    """How an already-open view reacts to a refresh event."""

    UPDATE = "update"
    ABSORB = "absorb"


PanelBuilder = Callable[[object, str], PanelDefinition | None]


@dataclass(frozen=True, slots=True)
class CommandPanelSpec:
    """One command feature's typed interactive panel contribution."""

    view_type: str
    view_model_type: type
    build: PanelBuilder
    refresh: PanelRefreshPolicy = PanelRefreshPolicy.UPDATE

    def build_for(self, model: object, title: str) -> PanelDefinition | None:
        """Build only when the event carries the declared ViewModel type."""
        if not isinstance(model, self.view_model_type):
            return None
        return self.build(model, title)


class CommandPanelRegistry:
    """Immutable lookup for command-owned panel contributions."""

    def __init__(self, specs: tuple[CommandPanelSpec, ...]) -> None:
        registered: dict[str, CommandPanelSpec] = {}
        for spec in specs:
            if spec.view_type in registered:
                raise ValueError(f"Duplicate command panel view: {spec.view_type}")
            registered[spec.view_type] = spec
        self._specs = registered

    def get(self, view_type: str) -> CommandPanelSpec | None:
        """Return the contribution for a semantic view type."""
        return self._specs.get(view_type)

    def view_types(self) -> tuple[str, ...]:
        """Return registered view types in stable contribution order."""
        return tuple(self._specs)


__all__ = [
    "CommandPanelRegistry",
    "CommandPanelSpec",
    "PanelDefinition",
    "PanelItem",
    "PanelRefreshPolicy",
]
