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
    MCP = "mcp"
    AGENT = "agent"


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


class AgentEventBridge:
    """Republish domain-level agent events onto the UI event bus."""

    def __init__(self, bus: UIEventBus):
        self.bus = bus

    def on_agent_event(self, event: AgentEvent) -> None:
        """Translate an agent event into a UI event envelope."""
        level = UIEventLevel.INFO
        if event.event_type == AgentEventType.ERROR:
            level = UIEventLevel.ERROR
        elif event.event_type in (AgentEventType.TOOL_CALL_START, AgentEventType.TOOL_CALL_END):
            level = UIEventLevel.DEBUG

        self.bus.emit(
            UIEvent(
                message=event.event_type.value,
                level=level,
                kind=UIEventKind.AGENT,
                data={"agent_event": event},
            )
        )
