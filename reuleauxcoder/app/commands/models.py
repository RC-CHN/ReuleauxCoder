"""Command models and shared execution result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from reuleauxcoder.interfaces.events import UIEvent
from reuleauxcoder.interfaces.interactions import UIInteractor


@dataclass(slots=True)
class OpenViewRequest:
    """Structured request for UI layers to open or focus a view."""

    view_type: str
    title: str
    payload: dict[str, Any] = field(default_factory=dict)
    focus: bool = True
    reuse_key: str | None = None


@dataclass(slots=True)
class CommandResult:
    """Common result returned by shared command handlers."""

    action: Literal["continue", "chat", "exit"] = "continue"
    session_id: str | None = None
    session_exit_time: str | None = None
    notifications: list[UIEvent] = field(default_factory=list)
    view_requests: list[OpenViewRequest] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ShowModelCommand:
    """Show configured model profiles and current active profile."""


@dataclass(slots=True)
class SwitchModelCommand:
    """Switch the runtime to a configured model profile."""

    profile_name: str


Command = ShowModelCommand | SwitchModelCommand


@dataclass(slots=True)
class CommandContext:
    """Shared runtime context passed to command handlers."""

    agent: Any
    config: Any
    ui_bus: Any
    ui_interactor: UIInteractor | None = None
