"""UI event bus and notification models for interface-layer output."""

from __future__ import annotations

import inspect
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, TypeAlias

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.runtime.events import (
    OperationPhaseChanged,
    RuntimeEvent,
    RuntimeEventDeliveryClass,
    agent_event_to_runtime_event,
    runtime_event_delivery_class,
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
    generation_owner_agent_id: str | None = None


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

_MAX_PENDING_RUNTIME_ISSUES = 8
_MAX_RUNTIME_ISSUE_COUNT = 1_000_000


class RuntimeIssueSink(Protocol):
    """Route one content-free failure fact to its owning runtime."""

    def __call__(
        self,
        phase: str,
        error_type: str,
        ref: str,
        count: int = 1,
        *,
        agent_id: str | None = None,
        session_generation: int | None = None,
    ) -> object: ...


class RuntimeIssueRoutingUnsupported(TypeError):
    """The sink cannot safely accept an explicitly routed failure fact."""


class UIEventDeliveryAck:
    """Marker for a subscriber's authoritative UI delivery result."""

    __slots__ = ()

    accepted: bool
    reason: object | None


@dataclass(frozen=True, slots=True)
class RuntimeIssueFact:
    """Bounded bus-local fallback retained while an owner sink is unavailable."""

    phase: str
    error_type: str
    ref: str
    count: int = 1
    agent_id: str | None = None
    session_generation: int | None = None


def deliver_runtime_issue(
    sink: RuntimeIssueSink,
    phase: str,
    error_type: str,
    ref: str,
    count: int = 1,
    *,
    agent_id: str | None = None,
    session_generation: int | None = None,
) -> object:
    """Call a sink once; explicit routes never degrade to an unrouted call."""
    route_required = agent_id is not None or session_generation is not None
    try:
        parameters = inspect.signature(sink).parameters
        supports_routing = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ) or {"agent_id", "session_generation"}.issubset(parameters)
    except KeyboardInterrupt:
        raise
    except BaseException:
        if route_required:
            raise RuntimeIssueRoutingUnsupported from None
        return sink(phase, error_type, ref, count)
    if supports_routing:
        return sink(
            phase,
            error_type,
            ref,
            count,
            agent_id=agent_id,
            session_generation=session_generation,
        )
    if route_required:
        raise RuntimeIssueRoutingUnsupported
    return sink(phase, error_type, ref, count)


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name and len(name) <= 64 and name.isascii() and name.replace("_", "").isalnum():
        return name
    return "Exception"


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
    emitting thread when queued. Ordinary return values are ignored; explicit
    ``UIEventDeliveryAck`` values are aggregated for synchronous required
    delivery.
    """

    def __init__(
        self,
        *,
        event_queue: queue.Queue | None = None,
        max_history: int = 256,
        subscriber_failure_sink: RuntimeIssueSink | None = None,
    ):
        if max_history < 1:
            raise ValueError("max_history must be positive")
        self._queue = event_queue
        self._handlers: list[Callable[[UIEvent], object]] = []
        self._history: list[UIEvent] = []
        self._max_history = max_history
        self._subscriber_failure_lock = threading.Lock()
        self._subscriber_failure_default_sink = subscriber_failure_sink
        self._subscriber_failure_sinks: dict[str, RuntimeIssueSink] = {}
        self._pending_subscriber_failures: list[RuntimeIssueFact] = []
        self._pending_subscriber_failure_overflow = 0
        self._subscriber_failure_stale_dropped = 0

    @property
    def is_queued(self) -> bool:
        """True when this bus uses cross-thread queued delivery."""
        return self._queue is not None

    def history_snapshot(self) -> tuple[UIEvent, ...]:
        """Return initialization events without exposing mutable bus history."""
        return tuple(self._history)

    def bind_subscriber_failure_sink(
        self,
        sink: RuntimeIssueSink | None,
        *,
        agent_id: str | None = None,
        default: bool = False,
    ) -> None:
        """Bind one owner sink and replay its bounded pending failure facts."""
        with self._subscriber_failure_lock:
            if agent_id is None or default:
                self._subscriber_failure_default_sink = sink
            if agent_id is not None:
                if sink is None:
                    self._subscriber_failure_sinks.pop(agent_id, None)
                else:
                    self._subscriber_failure_sinks[agent_id] = sink
        if sink is not None:
            self._replay_subscriber_failures(sink, agent_id)
            if default and agent_id is not None:
                self._replay_subscriber_failures(sink, None)

    def unbind_subscriber_failure_sink(self, *, agent_id: str) -> None:
        """Forget a routed owner without attributing later events elsewhere."""
        with self._subscriber_failure_lock:
            self._subscriber_failure_sinks.pop(agent_id, None)

    def subscriber_failure_snapshot(self) -> tuple[RuntimeIssueFact, ...]:
        """Expose bounded non-recursive fallback facts for diagnostics/tests."""
        with self._subscriber_failure_lock:
            facts = tuple(self._pending_subscriber_failures)
            overflow = self._pending_subscriber_failure_overflow
        if overflow:
            facts += (
                RuntimeIssueFact(
                    "ui_subscriber",
                    "Overflow",
                    "capacity",
                    count=overflow,
                ),
            )
        return facts

    @property
    def subscriber_failure_stale_dropped(self) -> int:
        """Count route-rejected stale facts without presenting them as faults."""
        with self._subscriber_failure_lock:
            return self._subscriber_failure_stale_dropped

    def subscribe(
        self,
        handler: Callable[[UIEvent], object],
        *,
        replay_history: bool = True,
    ) -> None:
        self._handlers.append(handler)
        if replay_history:
            for event in self._history:
                self._invoke_handler(handler, event, ref="history_replay")

    def emit(self, event: UIEvent) -> UIEventDeliveryAck | None:
        payload = event.payload
        runtime_payload = (
            payload.event.payload if isinstance(payload, RuntimeEventPayload) else None
        )
        replayable = not (
            runtime_payload is not None
            and (
                isinstance(runtime_payload, OperationPhaseChanged)
                or runtime_event_delivery_class(runtime_payload)
                is RuntimeEventDeliveryClass.TRANSIENT
            )
        )
        if replayable:
            self._history.append(event)
            if len(self._history) > self._max_history:
                del self._history[: -self._max_history]
        if self._queue is not None:
            self._queue.put(event)
            return None
        return self._dispatch(event)

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

    def _dispatch(self, event: UIEvent) -> UIEventDeliveryAck | None:
        """Call every registered handler for *event*."""
        accepted: UIEventDeliveryAck | None = None
        rejected: UIEventDeliveryAck | None = None
        for handler in self._handlers:
            result = self._invoke_handler(handler, event, ref="dispatch")
            if not isinstance(result, UIEventDeliveryAck):
                continue
            if result.accepted:
                accepted = accepted or result
            else:
                rejected = rejected or result
        return rejected or accepted

    def _invoke_handler(
        self,
        handler: Callable[[UIEvent], object],
        event: UIEvent,
        *,
        ref: str,
    ) -> object | None:
        try:
            return handler(event)
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._record_subscriber_failure(error, event=event, ref=ref)
            return None

    def emit_required(self, event: UIEvent) -> UIEventDeliveryAck | None:
        """Synchronously dispatch a required event and return an explicit ack."""
        if self._queue is not None:
            return None
        return self._dispatch(event)

    def report_runtime_issue(
        self,
        phase: str,
        error_type: str,
        ref: str,
    ) -> None:
        """Route one safe, unrouted interface failure through the default sink."""
        fact = RuntimeIssueFact(phase, error_type, ref)
        with self._subscriber_failure_lock:
            sink = self._subscriber_failure_default_sink
        if sink is None:
            self._retain_subscriber_failure(fact)
        else:
            self._deliver_subscriber_failure(sink, fact, replay=True)

    @staticmethod
    def _subscriber_failure_route(
        event: UIEvent,
    ) -> tuple[str | None, int | None]:
        payload = event.payload
        if not isinstance(payload, RuntimeEventPayload):
            return None, None
        runtime = payload.event
        generation = runtime.session_generation
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = None
        return payload.generation_owner_agent_id or runtime.agent_id, generation

    def _record_subscriber_failure(
        self,
        error: BaseException,
        *,
        event: UIEvent,
        ref: str,
    ) -> None:
        agent_id, session_generation = self._subscriber_failure_route(event)
        fact = RuntimeIssueFact(
            "ui_subscriber",
            _safe_error_type(error),
            ref,
            agent_id=agent_id,
            session_generation=session_generation,
        )
        with self._subscriber_failure_lock:
            sink = (
                self._subscriber_failure_sinks.get(agent_id)
                if agent_id is not None
                else self._subscriber_failure_default_sink
            )
        if sink is None:
            self._retain_subscriber_failure(fact)
            return
        self._deliver_subscriber_failure(sink, fact, replay=True)

    def _deliver_subscriber_failure(
        self,
        sink: RuntimeIssueSink,
        fact: RuntimeIssueFact,
        *,
        replay: bool,
    ) -> None:
        try:
            accepted = deliver_runtime_issue(
                sink,
                fact.phase,
                fact.error_type,
                fact.ref,
                fact.count,
                agent_id=fact.agent_id,
                session_generation=fact.session_generation,
            )
        except KeyboardInterrupt:
            raise
        except BaseException as error:
            self._retain_subscriber_failure(fact)
            self._retain_subscriber_failure(
                RuntimeIssueFact(
                    "ui_subscriber_sink",
                    _safe_error_type(error),
                    "delivery",
                    agent_id=fact.agent_id,
                    session_generation=fact.session_generation,
                )
            )
            return
        if accepted is False:
            with self._subscriber_failure_lock:
                self._subscriber_failure_stale_dropped = min(
                    self._subscriber_failure_stale_dropped + fact.count,
                    _MAX_RUNTIME_ISSUE_COUNT,
                )
        if replay:
            self._replay_subscriber_failures(
                sink, fact.agent_id, fact.session_generation
            )

    def _retain_subscriber_failure(self, fact: RuntimeIssueFact) -> None:
        count = (
            min(fact.count, _MAX_RUNTIME_ISSUE_COUNT)
            if isinstance(fact.count, int)
            and not isinstance(fact.count, bool)
            and fact.count > 0
            else 1
        )
        retained = RuntimeIssueFact(
            fact.phase,
            fact.error_type,
            fact.ref,
            count,
            fact.agent_id,
            fact.session_generation,
        )
        with self._subscriber_failure_lock:
            if len(self._pending_subscriber_failures) < _MAX_PENDING_RUNTIME_ISSUES - 1:
                self._pending_subscriber_failures.append(retained)
            else:
                self._pending_subscriber_failure_overflow = min(
                    self._pending_subscriber_failure_overflow + count,
                    _MAX_RUNTIME_ISSUE_COUNT,
                )

    def _replay_subscriber_failures(
        self,
        sink: RuntimeIssueSink,
        agent_id: str | None,
        session_generation: int | None = None,
    ) -> None:
        with self._subscriber_failure_lock:
            pending = tuple(
                fact
                for fact in self._pending_subscriber_failures
                if fact.agent_id == agent_id
                and (
                    session_generation is None
                    or fact.session_generation == session_generation
                )
            )
            self._pending_subscriber_failures = [
                fact
                for fact in self._pending_subscriber_failures
                if fact not in pending
            ]
        for fact in pending:
            self._deliver_subscriber_failure(sink, fact, replay=False)

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
                payload=RuntimeEventPayload(
                    event,
                    generation_owner_agent_id=event.agent_id,
                ),
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
        """Publish one operation lifecycle transition."""
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
        from reuleauxcoder.domain.process_output import terminal_safe_display

        self.emit(
            UIEvent.info(
                "",
                kind=UIEventKind.REMOTE,
                payload=RemoteStreamPayload(
                    tool_name,
                    stream,
                    terminal_safe_display(chunk),
                ),
            )
        )

    def emit_interaction_prompt(
        self,
        request: InteractionRequest,
    ) -> UIEventDeliveryAck | None:
        return self.emit_required(
            UIEvent.info(
                request.title,
                kind=UIEventKind.APPROVAL,
                payload=InteractionPromptPayload(request),
            )
        )


class AgentEventBridge:
    """Republish domain-level agent events onto the UI event bus."""

    def __init__(
        self,
        bus: UIEventBus,
        *,
        generation_owner_agent_id: str | None = None,
    ):
        self.bus = bus
        self.generation_owner_agent_id = generation_owner_agent_id

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
                payload=RuntimeEventPayload(
                    runtime_event,
                    generation_owner_agent_id=(
                        self.generation_owner_agent_id or runtime_event.agent_id
                    ),
                ),
            )
        )
