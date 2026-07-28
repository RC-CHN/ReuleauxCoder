"""Shared UI interaction protocols and request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from reuleauxcoder.domain.approval import ApprovalSection

if TYPE_CHECKING:
    from reuleauxcoder.interfaces.events import UIEvent


@dataclass(slots=True)
class ConfirmRequest:
    """Simple yes/no confirmation request."""

    title: str
    message: str
    severity: str = "info"
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    deadline: float | None = None


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


@dataclass(slots=True)
class ChooseOneRequest:
    """Request to choose one item from a list."""

    title: str
    items: list[ChoiceItem]
    message: str | None = None
    initial_id: str | None = None
    allow_cancel: bool = True
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    deadline: float | None = None


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
    secret: bool = False
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    deadline: float | None = None

    def __post_init__(self) -> None:
        if self.secret and self.initial_value:
            raise ValueError("secret text input cannot expose an initial value")


@dataclass(slots=True)
class InputTextResponse:
    """Response to a text-input interaction."""

    value: str | None
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class ReviewContext:
    tool_name: str
    tool_source: str
    operation: str | None = None
    subjects: tuple[str, ...] = ()
    reason: str | None = None
    is_subagent: bool = False
    subagent_mode: str | None = None
    subagent_task: str | None = None


ReviewAction = Literal["allow_once", "allow_session", "deny"]


@dataclass(frozen=True, slots=True)
class ReviewGrantOption:
    """One opaque, domain-validated session scope shown by a review UI."""

    id: str
    label: str
    description: str
    broad: bool = False


@dataclass(slots=True)
class ReviewRequest:
    """Structured review/approval request with optional preview sections."""

    title: str
    summary: str
    approve_label: str = "Approve"
    reject_label: str = "Reject"
    sections: tuple[ApprovalSection, ...] = ()
    context: ReviewContext | None = None
    grant_options: tuple[ReviewGrantOption, ...] = ()
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    deadline: float | None = None


@dataclass(slots=True)
class ReviewResponse:
    """Response to a structured review request."""

    approved: bool
    cancelled: bool = False
    reason: str | None = None
    action: ReviewAction | None = None
    selected_id: str | None = None

    def __post_init__(self) -> None:
        if self.action is None:
            self.action = "allow_once" if self.approved else "deny"


InteractionRequest: TypeAlias = (
    ConfirmRequest | ChooseOneRequest | InputTextRequest | ReviewRequest
)


class UIInteractor(Protocol):
    """Interface-layer interaction port for synchronous user input."""

    def notify(self, event: "UIEvent") -> None:
        """Optional direct notification hook for interfaces that need it."""
        ...

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        """Ask the user to confirm a yes/no decision."""
        ...

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        """Ask the user to choose one option."""
        ...

    def input_text(self, request: InputTextRequest) -> InputTextResponse:
        """Ask the user to input free-form text."""
        ...

    def review(self, request: ReviewRequest) -> ReviewResponse:
        """Ask the user to review structured content and approve/reject it."""
        ...
