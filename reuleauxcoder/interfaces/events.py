"""UI event bus and notification models for interface-layer output."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType


class UIEventLevel(Enum):
    """Visual severity / style for UI events."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    DEBUG = "debug"


class UIEventKind(Enum):
    """Logical kind for interface-layer events."""

    SYSTEM = "system"
    COMMAND = "command"
    SESSION = "session"
    MODEL = "model"
    MCP = "mcp"
    APPROVAL = "approval"
    VIEW = "view"
    AGENT = "agent"
    CONTEXT = "context"
    REMOTE = "remote"


@dataclass
class UIEvent:
    """A user-facing event emitted through the UI bus."""

    message: str
    level: UIEventLevel = UIEventLevel.INFO
    kind: UIEventKind = UIEventKind.SYSTEM
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def info(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.INFO, kind=kind, data=data)

    @classmethod
    def success(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.SUCCESS, kind=kind, data=data)

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.WARNING, kind=kind, data=data)

    @classmethod
    def error(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.ERROR, kind=kind, data=data)

    @classmethod
    def debug(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> "UIEvent":
        return cls(message=message, level=UIEventLevel.DEBUG, kind=kind, data=data)


class UIEventBus:
    """Simple synchronous publish/subscribe bus for UI events."""

    def __init__(self):
        self._handlers: list[Callable[[UIEvent], None]] = []
        self._history: list[UIEvent] = []

    def subscribe(
        self,
        handler: Callable[[UIEvent], None],
        *,
        replay_history: bool = True,
    ) -> None:
        self._handlers.append(handler)
        if replay_history:
            for event in self._history:
                try:
                    handler(event)
                except Exception:
                    pass

    def emit(self, event: UIEvent) -> None:
        self._history.append(event)
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                pass

    def info(self, message: str, *, kind: UIEventKind = UIEventKind.SYSTEM, **data: Any) -> None:
        self.emit(UIEvent.info(message, kind=kind, **data))

    def success(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.success(message, kind=kind, **data))

    def warning(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.warning(message, kind=kind, **data))

    def error(self, message: str, *, kind: UIEventKind = UIEventKind.SYSTEM, **data: Any) -> None:
        self.emit(UIEvent.error(message, kind=kind, **data))

    def debug(self, message: str, *, kind: UIEventKind = UIEventKind.SYSTEM, **data: Any) -> None:
        self.emit(UIEvent.debug(message, kind=kind, **data))

    def open_view(
        self,
        view_type: str,
        *,
        title: str,
        payload: dict[str, Any] | None = None,
        focus: bool = True,
        reuse_key: str | None = None,
    ) -> None:
        """Broadcast a structured request for the UI to open a view/panel/tab."""
        self.emit(
            UIEvent.info(
                f"Open view: {title}",
                kind=UIEventKind.VIEW,
                action="open",
                view_type=view_type,
                title=title,
                payload=payload or {},
                focus=focus,
                reuse_key=reuse_key,
            )
        )

    def refresh_view(
        self,
        view_type: str,
        *,
        title: str | None = None,
        payload: dict[str, Any] | None = None,
        reuse_key: str | None = None,
    ) -> None:
        """Broadcast a structured request for the UI to refresh a view."""
        self.emit(
            UIEvent.info(
                f"Refresh view: {title or view_type}",
                kind=UIEventKind.VIEW,
                action="refresh",
                view_type=view_type,
                title=title or view_type,
                payload=payload or {},
                reuse_key=reuse_key,
            )
        )


class AgentEventBridge:
    """Republish domain-level agent events onto the UI event bus."""

    def __init__(self, bus: UIEventBus):
        self.bus = bus

    def on_agent_event(self, event: AgentEvent) -> None:
        """Translate an agent event into a UI event envelope."""
        level = UIEventLevel.INFO
        if event.event_type == AgentEventType.ERROR:
            level = UIEventLevel.ERROR
        elif event.event_type in (
            AgentEventType.TOOL_CALL_START,
            AgentEventType.TOOL_CALL_END,
            AgentEventType.SUBAGENT_COMPLETED,
        ):
            level = UIEventLevel.DEBUG

        self.bus.emit(
            UIEvent(
                message=event.event_type.value,
                level=level,
                kind=UIEventKind.AGENT,
                data={
                    "agent_event": event,
                    "event_type": event.event_type.value,
                    "tool_name": event.tool_name,
                    "tool_args": event.tool_args,
                    "tool_result": event.tool_result,
                    "tool_success": event.tool_success,
                    "error_message": event.error_message,
                },
            )
        )

