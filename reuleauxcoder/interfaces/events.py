"""UI event bus and notification models for interface-layer output."""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeAlias

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.runtime.events import (
    OperationPhaseChanged,
    RuntimeEvent,
    agent_event_to_runtime_event,
    is_transient_runtime_payload,
)
from reuleauxcoder.interfaces.interactions import InteractionRequest


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


class ViewModelPort(Protocol):
    view_type: str


@dataclass(frozen=True, slots=True)
class RuntimeEventPayload:
    event: RuntimeEvent


@dataclass(frozen=True, slots=True)
class ViewEventPayload:
    action: str
    view_type: str
    title: str
    view_model: ViewModelPort
    focus: bool = True
    reuse_key: str | None = None


@dataclass(frozen=True, slots=True)
class RemoteStreamPayload:
    tool_name: str
    stream: str
    chunk: str


@dataclass(frozen=True, slots=True)
class ReasoningNoticePayload:
    title: str = "Reasoning"


@dataclass(frozen=True, slots=True)
class InteractionPromptPayload:
    request: InteractionRequest


UIEventPayload: TypeAlias = (
    RuntimeEventPayload
    | ViewEventPayload
    | RemoteStreamPayload
    | ReasoningNoticePayload
    | InteractionPromptPayload
)


@dataclass
class UIEvent:
    """A user-facing event emitted through the UI bus."""

    message: str
    level: UIEventLevel = UIEventLevel.INFO
    kind: UIEventKind = UIEventKind.SYSTEM
    timestamp: float = field(default_factory=time.time)
    payload: UIEventPayload | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def info(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> "UIEvent":
        return cls(
            message=message,
            level=UIEventLevel.INFO,
            kind=kind,
            payload=payload,
            data=data,
        )

    @classmethod
    def success(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> "UIEvent":
        return cls(
            message=message,
            level=UIEventLevel.SUCCESS,
            kind=kind,
            payload=payload,
            data=data,
        )

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> "UIEvent":
        return cls(
            message=message,
            level=UIEventLevel.WARNING,
            kind=kind,
            payload=payload,
            data=data,
        )

    @classmethod
    def error(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> "UIEvent":
        return cls(
            message=message,
            level=UIEventLevel.ERROR,
            kind=kind,
            payload=payload,
            data=data,
        )

    @classmethod
    def debug(
        cls,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> "UIEvent":
        return cls(
            message=message,
            level=UIEventLevel.DEBUG,
            kind=kind,
            payload=payload,
            data=data,
        )


class UIEventBus:
    """Publish/subscribe bus for UI events.

    Two delivery modes:

    * **Synchronous** (default) — ``emit()`` calls every handler immediately
      on the calling thread.  Used by CLI (single-thread).
    * **Queued** — pass a ``queue.Queue`` at construction time.  ``emit()``
      pushes events onto the queue; the UI thread must periodically call
      ``drain()`` to dispatch them.  Used by TUI (cross-thread).

    Handlers are always called on the **draining thread** — never on the
    emitting thread when queued.
    """

    def __init__(self, *, event_queue: queue.Queue | None = None):
        self._queue = event_queue
        self._handlers: list[Callable[[UIEvent], None]] = []
        self._history: list[UIEvent] = []

    @property
    def is_queued(self) -> bool:
        """True when this bus uses cross-thread queued delivery."""
        return self._queue is not None

    def history_snapshot(self) -> tuple[UIEvent, ...]:
        """Return initialization events without exposing mutable bus history."""
        return tuple(self._history)

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
        payload = event.payload
        if not (
            isinstance(payload, RuntimeEventPayload)
            and is_transient_runtime_payload(payload.event.payload)
        ):
            self._history.append(event)
        if self._queue is not None:
            self._queue.put(event)
        else:
            self._dispatch(event)

    def drain(self) -> None:
        """Dequeue and dispatch all pending events (queued mode only).

        Call periodically from the UI main thread (e.g. via
        ``set_interval``).  No-op in synchronous mode.
        """
        if self._queue is None:
            return
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            self._dispatch(event)

    def _dispatch(self, event: UIEvent) -> None:
        """Call every registered handler for *event*."""
        for handler in self._handlers:
            try:
                handler(event)
            except Exception:
                pass

    def info(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.info(message, kind=kind, payload=payload, **data))

    def success(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.success(message, kind=kind, payload=payload, **data))

    def warning(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.warning(message, kind=kind, payload=payload, **data))

    def error(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.error(message, kind=kind, payload=payload, **data))

    def debug(
        self,
        message: str,
        *,
        kind: UIEventKind = UIEventKind.SYSTEM,
        payload: UIEventPayload | None = None,
        **data: Any,
    ) -> None:
        self.emit(UIEvent.debug(message, kind=kind, payload=payload, **data))

    def emit_runtime(self, event: RuntimeEvent) -> None:
        """Publish one typed runtime event through the shared UI scheduler."""
        self.emit(
            UIEvent(
                message=event.kind.value,
                kind=UIEventKind.AGENT,
                payload=RuntimeEventPayload(event),
            )
        )

    def emit_operation_phase(
        self,
        *,
        operation_id: str,
        operation: str,
        phase: str,
        status: str = "running",
        detail: str | None = None,
        started_at: float | None = None,
        elapsed_ms: int | None = None,
        attempt: int | None = None,
        max_attempts: int | None = None,
        cancelable: bool = False,
        endpoint_host: str | None = None,
        error_type: str | None = None,
        agent_id: str | None = None,
        session_generation: int | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        """Publish one non-replayable operation lifecycle transition."""
        self.emit_runtime(
            RuntimeEvent(
                payload=OperationPhaseChanged(
                    operation_id=operation_id,
                    operation=operation,
                    phase=phase,
                    status=status,
                    detail=detail,
                    started_at=started_at,
                    elapsed_ms=elapsed_ms,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    cancelable=cancelable,
                    endpoint_host=endpoint_host,
                    error_type=error_type,
                ),
                agent_id=agent_id,
                session_generation=session_generation,
                session_id=session_id,
                turn_id=turn_id,
                correlation_id=operation_id,
            )
        )

    def open_view(
        self,
        view_type: str,
        *,
        title: str,
        focus: bool = True,
        reuse_key: str | None = None,
        view_model: ViewModelPort,
    ) -> None:
        """Broadcast a structured request for the UI to open a view/panel/tab."""
        if view_model.view_type != view_type:
            raise ValueError("view_type must match view_model.view_type")
        self.emit(
            UIEvent.info(
                f"Open view: {title}",
                kind=UIEventKind.VIEW,
                payload=ViewEventPayload(
                    action="open",
                    view_type=view_type,
                    title=title,
                    focus=focus,
                    reuse_key=reuse_key,
                    view_model=view_model,
                ),
            )
        )

    def refresh_view(
        self,
        view_type: str,
        *,
        title: str | None = None,
        reuse_key: str | None = None,
        view_model: ViewModelPort,
    ) -> None:
        """Broadcast a structured request for the UI to refresh a view."""
        if view_model.view_type != view_type:
            raise ValueError("view_type must match view_model.view_type")
        self.emit(
            UIEvent.info(
                f"Refresh view: {title or view_type}",
                kind=UIEventKind.VIEW,
                payload=ViewEventPayload(
                    action="refresh",
                    view_type=view_type,
                    title=title or view_type,
                    focus=False,
                    reuse_key=reuse_key,
                    view_model=view_model,
                ),
            )
        )

    def emit_remote_stream(self, *, tool_name: str, stream: str, chunk: str) -> None:
        self.emit(
            UIEvent.info(
                "",
                kind=UIEventKind.REMOTE,
                payload=RemoteStreamPayload(tool_name, stream, chunk),
            )
        )

    def emit_interaction_prompt(self, request: InteractionRequest) -> None:
        self.emit(
            UIEvent.info(
                request.title,
                kind=UIEventKind.APPROVAL,
                payload=InteractionPromptPayload(request),
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
            AgentEventType.TOOL_OUTPUT_DELTA,
            AgentEventType.TOOL_CALL_END,
            AgentEventType.SUBAGENT_COMPLETED,
            AgentEventType.APPROVAL_REQUESTED,
            AgentEventType.APPROVAL_RESOLVED,
        ):
            level = UIEventLevel.DEBUG

        runtime_event = agent_event_to_runtime_event(event)
        self.bus.emit(
            UIEvent(
                message=event.event_type.value,
                level=level,
                kind=UIEventKind.AGENT,
                payload=RuntimeEventPayload(runtime_event),
            )
        )
