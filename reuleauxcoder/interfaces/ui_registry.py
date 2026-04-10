"""Shared UI registration primitives for interface composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.interfaces.interactions import UIInteractor
    from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry


class UICapability(str, Enum):
    """Declared capability supported by a UI implementation."""

    TEXT_INPUT = "text_input"
    STREAM_OUTPUT = "stream_output"
    PALETTE = "palette"
    BUTTONS = "buttons"
    MENUS = "menus"
    TABS = "tabs"
    MODAL = "modal"
    DIFF_REVIEW = "diff_review"
    TEXT_SELECT = "text_select"
    TEXT_EDIT = "text_edit"


@dataclass(frozen=True, slots=True)
class UIProfile:
    """Identity and capability declaration for a UI target."""

    ui_id: str
    display_name: str
    capabilities: frozenset[UICapability] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class UIRegistration:
    """Concrete UI registration containing its view and interaction adapters."""

    profile: UIProfile
    view_registry: ViewRendererRegistry
    interactor: UIInteractor


class UIRegistry:
    """Static registry of available UI implementations."""

    def __init__(self, registrations: list[UIRegistration]):
        self._registrations = {registration.profile.ui_id: registration for registration in registrations}

    def get(self, ui_id: str) -> UIRegistration | None:
        """Return a registered UI if present."""
        return self._registrations.get(ui_id)

    def require(self, ui_id: str) -> UIRegistration:
        """Return a registered UI or raise if it does not exist."""
        registration = self.get(ui_id)
        if registration is None:
            raise KeyError(f"Unknown UI: {ui_id}")
        return registration

    def list(self) -> list[UIRegistration]:
        """List registered UIs."""
        return list(self._registrations.values())
