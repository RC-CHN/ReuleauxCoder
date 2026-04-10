"""Shared UI interaction protocols and request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from reuleauxcoder.interfaces.events import UIEvent


@dataclass(slots=True)
class ConfirmRequest:
    """Simple yes/no confirmation request."""

    title: str
    message: str
    severity: str = "info"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConfirmResponse:
    """Response for a confirmation request."""

    confirmed: bool
    cancelled: bool = False


@dataclass(slots=True)
class ChoiceItem:
    """A single choice option presented to the UI."""

    id: str
    label: str
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChooseOneRequest:
    """Request to choose one item from a list."""

    title: str
    items: list[ChoiceItem]
    message: str | None = None
    initial_id: str | None = None
    allow_cancel: bool = True
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChooseOneResponse:
    """Response to a choose-one interaction."""

    selected_id: str | None
    cancelled: bool = False


@dataclass(slots=True)
class InputTextRequest:
    """Request for free-form text input."""

    title: str
    prompt: str
    initial_value: str = ""
    placeholder: str | None = None
    allow_empty: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InputTextResponse:
    """Response to a text-input interaction."""

    value: str | None
    cancelled: bool = False


@dataclass(slots=True)
class ReviewRequest:
    """Structured review/approval request with optional preview sections."""

    title: str
    summary: str
    approve_label: str = "Approve"
    reject_label: str = "Reject"
    sections: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReviewResponse:
    """Response to a structured review request."""

    approved: bool
    cancelled: bool = False
    reason: str | None = None


class UIInteractor(Protocol):
    """Interface-layer interaction port for synchronous user input."""

    def notify(self, event: UIEvent) -> None:
        """Optional direct notification hook for interfaces that need it."""

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        """Ask the user to confirm a yes/no decision."""

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        """Ask the user to choose one option."""

    def input_text(self, request: InputTextRequest) -> InputTextResponse:
        """Ask the user to input free-form text."""

    def review(self, request: ReviewRequest) -> ReviewResponse:
        """Ask the user to review structured content and approve/reject it."""
