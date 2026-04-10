"""Command models and shared execution result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
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
class ShowHelpCommand:
    """Show user-facing command help."""


@dataclass(slots=True)
class ExitCommand:
    """Exit the current interface, auto-saving if needed."""

    current_session_id: str | None = None


@dataclass(slots=True)
class ResetConversationCommand:
    """Clear current in-memory conversation only."""


@dataclass(slots=True)
class CompactContextCommand:
    """Compact the current conversation context."""


@dataclass(slots=True)
class ShowModelCommand:
    """Show configured model profiles and current active profile."""


@dataclass(slots=True)
class SwitchModelCommand:
    """Switch the runtime to a configured model profile."""

    profile_name: str


@dataclass(slots=True)
class ListSessionsCommand:
    """List saved sessions."""

    limit: int = 20


@dataclass(slots=True)
class ResumeSessionCommand:
    """Resume a saved session by ID or alias."""

    target: str


@dataclass(slots=True)
class SaveSessionCommand:
    """Persist the current session to disk."""

    current_session_id: str | None = None


@dataclass(slots=True)
class NewSessionCommand:
    """Start a new conversation, auto-saving the previous one if needed."""

    current_session_id: str | None = None


@dataclass(slots=True)
class ShowTokensCommand:
    """Show token usage and current context window budget."""


@dataclass(slots=True)
class ShowApprovalCommand:
    """Show approval rules and effective policy resolution."""


@dataclass(slots=True)
class SetApprovalRuleCommand:
    """Set or replace one approval rule via textual target/action syntax."""

    target: str
    action: str


@dataclass(slots=True)
class ShowMCPServersCommand:
    """Show configured MCP servers and current runtime state."""


@dataclass(slots=True)
class ToggleMCPServerCommand:
    """Enable or disable one MCP server."""

    server_name: str
    enabled: bool


Command = (
    ShowHelpCommand
    | ExitCommand
    | ResetConversationCommand
    | CompactContextCommand
    | ShowModelCommand
    | SwitchModelCommand
    | ListSessionsCommand
    | ResumeSessionCommand
    | SaveSessionCommand
    | NewSessionCommand
    | ShowTokensCommand
    | ShowApprovalCommand
    | SetApprovalRuleCommand
    | ShowMCPServersCommand
    | ToggleMCPServerCommand
)


@dataclass(slots=True)
class CommandContext:
    """Shared runtime context passed to command handlers."""

    agent: Any
    config: Any
    ui_bus: Any
    ui_interactor: UIInteractor | None = None
    sessions_dir: Path | None = None

