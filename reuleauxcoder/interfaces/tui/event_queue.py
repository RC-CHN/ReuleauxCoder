"""Bounded cross-thread event delivery for the terminal UI."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
import threading
import time

from reuleauxcoder.domain.runtime.events import (
    AssistantContentDelta,
    OperationPhaseChanged,
    ProcessSessionChanged,
    ReasoningDelta,
    RuntimeEventDeliveryClass,
    StreamChunk,
    ToolOutputDelta,
    runtime_event_delivery_class,
)
from reuleauxcoder.interfaces.events import (
    RemoteStreamPayload,
    RuntimeEventPayload,
    UIEvent,
    UIEventDeliveryAck,
)


# The TUI paints at up to 30 fps with a 50 ms maximum postpone interval.
# Two postponed paints therefore fit inside the 100 ms control wait, while
# 32 reserved controls and 224 transient keys cover a much larger single-paint
# burst without copying Crush's remote-subscriber constants.
DEFAULT_EVENT_QUEUE_CAPACITY = 256
DEFAULT_CONTROL_RESERVE = 32
DEFAULT_MUST_DELIVER_TIMEOUT_SECONDS = 0.1
DEFAULT_MAX_COALESCED_CHARS = 64 * 1024
_MAX_STALE_GENERATION_DROPS = 1_000_000

_TEXT_RUNTIME_PAYLOADS = (
    AssistantContentDelta,
    ReasoningDelta,
    StreamChunk,
    ToolOutputDelta,
)


@dataclass(frozen=True, slots=True)
class EventQueueStats:
    capacity: int
    control_reserve: int
    max_coalesced_chars: int
    depth: int
    transient_depth: int
    high_watermark: int
    accepted: int
    dequeued: int
    coalesced: int
    transient_dropped: int
    must_deliver_waits: int
    must_deliver_timeouts: int
    closed_dropped: int
    stale_generation_dropped: int
    closed: bool

    @property
    def dropped(self) -> int:
        return (
            self.transient_dropped
            + self.must_deliver_timeouts
            + self.closed_dropped
            + self.stale_generation_dropped
        )


@dataclass(frozen=True, slots=True)
class EventPutResult(UIEventDeliveryAck):
    accepted: bool
    wake_consumer: bool = False
    coalesced: bool = False
    reason: EventPutFailureReason | None = None


class EventPutFailureReason(str, Enum):
    """Stable, non-sensitive reason for a rejected event."""

    CLOSED = "closed"
    CONTROL_TIMEOUT = "control_timeout"
    TRANSIENT_CAPACITY = "transient_capacity"
    STALE_GENERATION = "stale_generation"


class BoundedUIEventQueue:
    """Bound transient traffic while giving control events reserved capacity.

    Transient events coalesce only after the most recent control barrier.
    When transient capacity is exhausted, the oldest transient is replaced by
    the newest one. Control events first reclaim a transient slot, then wait
    for a bounded interval if the queue contains control events only. The queue
    caps both retained references and text it concatenates; canonical control
    payloads remain unmodified and are never copied into queue-owned buffers.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_EVENT_QUEUE_CAPACITY,
        control_reserve: int = DEFAULT_CONTROL_RESERVE,
        must_deliver_timeout: float = DEFAULT_MUST_DELIVER_TIMEOUT_SECONDS,
        max_coalesced_chars: int = DEFAULT_MAX_COALESCED_CHARS,
        generation_agent_id: str | None = None,
    ) -> None:
        if capacity < 2:
            raise ValueError("event queue capacity must be at least 2")
        if control_reserve < 1 or control_reserve >= capacity:
            raise ValueError("control reserve must be between 1 and capacity - 1")
        if must_deliver_timeout <= 0:
            raise ValueError("must-deliver timeout must be positive")
        if max_coalesced_chars < 1:
            raise ValueError("max coalesced chars must be positive")
        self.capacity = int(capacity)
        self.control_reserve = int(control_reserve)
        self.must_deliver_timeout = float(must_deliver_timeout)
        self.max_coalesced_chars = int(max_coalesced_chars)
        self._transient_capacity = self.capacity - self.control_reserve
        self._events: deque[UIEvent] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._transient_depth = 0
        self._high_watermark = 0
        self._accepted = 0
        self._dequeued = 0
        self._coalesced = 0
        self._transient_dropped = 0
        self._must_deliver_waits = 0
        self._must_deliver_timeouts = 0
        self._closed_dropped = 0
        self._stale_generation_dropped = 0
        self._minimum_generation: int | None = None
        self._generation_agent_id = generation_agent_id

    def put(
        self,
        event: UIEvent,
        *,
        timeout: float | None = None,
    ) -> EventPutResult:
        """Offer one event, blocking control delivery for at most ``timeout``."""
        transient = _is_transient_event(event)
        with self._condition:
            if self._closed:
                self._closed_dropped += 1
                return EventPutResult(False, reason=EventPutFailureReason.CLOSED)
            if self._is_stale_generation_locked(event):
                self._stale_generation_dropped = min(
                    self._stale_generation_dropped + 1,
                    _MAX_STALE_GENERATION_DROPS,
                )
                return EventPutResult(
                    False,
                    reason=EventPutFailureReason.STALE_GENERATION,
                )
            if transient:
                return self._put_transient_locked(event)

            wait_timeout = self.must_deliver_timeout if timeout is None else timeout
            if wait_timeout <= 0:
                raise ValueError("must-deliver timeout must be positive")
            deadline_at = time.monotonic() + wait_timeout
            waited = False
            while len(self._events) >= self.capacity:
                if self._evict_oldest_transient_locked():
                    break
                if not waited:
                    self._must_deliver_waits += 1
                    waited = True
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    self._must_deliver_timeouts += 1
                    return EventPutResult(
                        False,
                        reason=EventPutFailureReason.CONTROL_TIMEOUT,
                    )
                self._condition.wait(remaining)
                if self._closed:
                    self._closed_dropped += 1
                    return EventPutResult(
                        False,
                        reason=EventPutFailureReason.CLOSED,
                    )
            wake_consumer = self._append_locked(event, transient=False)
            return EventPutResult(True, wake_consumer=wake_consumer)

    def drain(self) -> list[UIEvent]:
        """Atomically remove all pending events and wake blocked producers."""
        with self._condition:
            events = list(self._events)
            self._events.clear()
            self._transient_depth = 0
            self._dequeued += len(events)
            if events:
                self._condition.notify_all()
            return events

    def close(self) -> None:
        """Reject new events and wake every blocked must-deliver producer."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def advance_generation(self, generation: int) -> int:
        """Raise the local route floor and discard queued events below it."""
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise TypeError("session generation must be an integer")
        with self._condition:
            if (
                self._minimum_generation is not None
                and generation < self._minimum_generation
            ):
                raise ValueError("session generation cannot move backwards")
            self._minimum_generation = generation
            retained = deque(
                event
                for event in self._events
                if not self._is_stale_generation_locked(event)
            )
            dropped = len(self._events) - len(retained)
            if not dropped:
                return 0
            self._events = retained
            self._transient_depth = sum(
                _is_transient_event(event) for event in retained
            )
            self._stale_generation_dropped = min(
                self._stale_generation_dropped + dropped,
                _MAX_STALE_GENERATION_DROPS,
            )
            self._condition.notify_all()
            return dropped

    def reject_stale(self, event: UIEvent) -> bool:
        """Account for an event drained just before the route floor advanced."""
        with self._condition:
            stale = self._is_stale_generation_locked(event)
            if stale:
                self._stale_generation_dropped = min(
                    self._stale_generation_dropped + 1,
                    _MAX_STALE_GENERATION_DROPS,
                )
            return stale

    def _is_stale_generation_locked(self, event: UIEvent) -> bool:
        generation_owner_agent_id, generation = _event_route(event)
        if generation is None or self._minimum_generation is None:
            return False
        if self._generation_agent_id is not None and generation_owner_agent_id not in {
            None,
            self._generation_agent_id,
        }:
            return False
        return generation < self._minimum_generation

    def stats(self) -> EventQueueStats:
        with self._condition:
            return EventQueueStats(
                capacity=self.capacity,
                control_reserve=self.control_reserve,
                max_coalesced_chars=self.max_coalesced_chars,
                depth=len(self._events),
                transient_depth=self._transient_depth,
                high_watermark=self._high_watermark,
                accepted=self._accepted,
                dequeued=self._dequeued,
                coalesced=self._coalesced,
                transient_dropped=self._transient_dropped,
                must_deliver_waits=self._must_deliver_waits,
                must_deliver_timeouts=self._must_deliver_timeouts,
                closed_dropped=self._closed_dropped,
                stale_generation_dropped=self._stale_generation_dropped,
                closed=self._closed,
            )

    def _put_transient_locked(self, event: UIEvent) -> EventPutResult:
        event, truncated = _bound_transient(
            event,
            max_chars=self.max_coalesced_chars,
        )
        if truncated:
            self._record_transient_drop_locked()
        key = _transient_key(event)
        if (
            key is not None
            and self._events
            and _is_transient_event(self._events[-1])
            and _transient_key(self._events[-1]) == key
        ):
            merged, truncated = _merge_transient(
                self._events[-1],
                event,
                max_chars=self.max_coalesced_chars,
            )
            self._events[-1] = merged
            self._accepted += 1
            self._coalesced += 1
            if truncated:
                self._record_transient_drop_locked()
            return EventPutResult(True, coalesced=True)

        at_transient_limit = self._transient_depth >= self._transient_capacity
        at_total_limit = len(self._events) >= self.capacity
        if at_transient_limit or at_total_limit:
            if not self._evict_oldest_transient_locked():
                self._record_transient_drop_locked()
                return EventPutResult(
                    False,
                    reason=EventPutFailureReason.TRANSIENT_CAPACITY,
                )
        wake_consumer = self._append_locked(event, transient=True)
        return EventPutResult(True, wake_consumer=wake_consumer)

    def _append_locked(self, event: UIEvent, *, transient: bool) -> bool:
        wake_consumer = not self._events
        self._events.append(event)
        if transient:
            self._transient_depth += 1
        self._accepted += 1
        self._high_watermark = max(self._high_watermark, len(self._events))
        self._condition.notify()
        return wake_consumer

    def _evict_oldest_transient_locked(self) -> bool:
        for index, event in enumerate(self._events):
            if not _is_transient_event(event):
                continue
            del self._events[index]
            self._transient_depth -= 1
            self._record_transient_drop_locked()
            return True
        return False

    def _record_transient_drop_locked(self) -> None:
        self._transient_dropped += 1


def _runtime_payload(event: UIEvent):
    envelope = event.payload
    if not isinstance(envelope, RuntimeEventPayload):
        return None
    return envelope.event.payload


def _event_route(event: UIEvent) -> tuple[str | None, int | None]:
    envelope = event.payload
    if not isinstance(envelope, RuntimeEventPayload):
        return None, None
    generation = envelope.event.session_generation
    if not isinstance(generation, int) or isinstance(generation, bool):
        generation = None
    return (
        envelope.generation_owner_agent_id or envelope.event.agent_id,
        generation,
    )


def _is_transient_event(event: UIEvent) -> bool:
    if isinstance(event.payload, RemoteStreamPayload):
        return True
    payload = _runtime_payload(event)
    if payload is None:
        return False
    return runtime_event_delivery_class(payload) is RuntimeEventDeliveryClass.TRANSIENT


def _transient_key(event: UIEvent) -> tuple | None:
    if isinstance(event.payload, RemoteStreamPayload):
        payload = event.payload
        return (RemoteStreamPayload, payload.tool_name, payload.stream)
    envelope = event.payload
    if not isinstance(envelope, RuntimeEventPayload):
        return None
    runtime = envelope.event
    payload = runtime.payload
    route = (
        runtime.agent_id,
        runtime.session_id,
        runtime.session_generation,
        runtime.turn_id,
    )
    if isinstance(payload, AssistantContentDelta):
        return (AssistantContentDelta, route, runtime.correlation_id)
    if isinstance(payload, ReasoningDelta):
        return (
            ReasoningDelta,
            route,
            runtime.correlation_id,
            payload.display_mode,
        )
    if isinstance(payload, StreamChunk):
        return (
            StreamChunk,
            route,
            runtime.correlation_id,
            payload.reasoning,
            payload.display_mode,
        )
    if isinstance(payload, ToolOutputDelta):
        return (
            ToolOutputDelta,
            route,
            payload.tool_call_id,
            payload.stream,
        )
    if isinstance(payload, OperationPhaseChanged):
        return (OperationPhaseChanged, route, payload.operation_id)
    if isinstance(payload, ProcessSessionChanged):
        return (ProcessSessionChanged, route, payload.process_session_id)
    return None


def _merge_transient(
    previous: UIEvent,
    current: UIEvent,
    *,
    max_chars: int,
) -> tuple[UIEvent, bool]:
    if isinstance(previous.payload, RemoteStreamPayload) and isinstance(
        current.payload, RemoteStreamPayload
    ):
        combined = previous.payload.chunk + current.payload.chunk
        truncated = len(combined) > max_chars
        payload = replace(
            current.payload,
            chunk=combined[-max_chars:],
        )
        return replace(current, payload=payload), truncated

    previous_envelope = previous.payload
    current_envelope = current.payload
    if not isinstance(previous_envelope, RuntimeEventPayload) or not isinstance(
        current_envelope, RuntimeEventPayload
    ):
        return current, False
    previous_payload = previous_envelope.event.payload
    current_runtime = current_envelope.event
    current_payload = current_runtime.payload
    if isinstance(previous_payload, _TEXT_RUNTIME_PAYLOADS) and isinstance(
        current_payload, type(previous_payload)
    ):
        combined = previous_payload.text + current_payload.text
        truncated = len(combined) > max_chars
        payload = replace(
            current_payload,
            text=combined[-max_chars:],
        )
        runtime = replace(current_runtime, payload=payload)
        return replace(
            current,
            payload=replace(current_envelope, event=runtime),
        ), truncated
    if isinstance(previous_payload, ProcessSessionChanged) and isinstance(
        current_payload, ProcessSessionChanged
    ):
        stdout = previous_payload.stdout + current_payload.stdout
        stderr = previous_payload.stderr + current_payload.stderr
        truncated = len(stdout) > max_chars or len(stderr) > max_chars
        payload = replace(
            current_payload,
            stdout=stdout[-max_chars:],
            stderr=stderr[-max_chars:],
            output_truncated=(
                previous_payload.output_truncated
                or current_payload.output_truncated
                or truncated
            ),
            output_decode_replaced=(
                previous_payload.output_decode_replaced
                or current_payload.output_decode_replaced
            ),
        )
        runtime = replace(current_runtime, payload=payload)
        return replace(
            current,
            payload=replace(current_envelope, event=runtime),
        ), truncated
    return current, False


def _bound_transient(event: UIEvent, *, max_chars: int) -> tuple[UIEvent, bool]:
    payload = event.payload
    if isinstance(payload, RemoteStreamPayload):
        if len(payload.chunk) <= max_chars:
            return event, False
        return replace(
            event, payload=replace(payload, chunk=payload.chunk[-max_chars:])
        ), True
    if not isinstance(payload, RuntimeEventPayload):
        return event, False
    runtime = payload.event
    runtime_payload = runtime.payload
    bounded = runtime_payload
    truncated = False
    if isinstance(runtime_payload, _TEXT_RUNTIME_PAYLOADS):
        if len(runtime_payload.text) > max_chars:
            bounded = replace(runtime_payload, text=runtime_payload.text[-max_chars:])
            truncated = True
    elif isinstance(runtime_payload, OperationPhaseChanged):
        if runtime_payload.detail and len(runtime_payload.detail) > max_chars:
            bounded = replace(
                runtime_payload,
                detail=runtime_payload.detail[-max_chars:],
            )
            truncated = True
    elif isinstance(runtime_payload, ProcessSessionChanged):
        stdout = runtime_payload.stdout[-max_chars:]
        stderr = runtime_payload.stderr[-max_chars:]
        truncated = (
            len(runtime_payload.stdout) > max_chars
            or len(runtime_payload.stderr) > max_chars
        )
        if truncated:
            bounded = replace(
                runtime_payload,
                stdout=stdout,
                stderr=stderr,
                output_truncated=True,
            )
    if not truncated:
        return event, False
    return (
        replace(
            event,
            payload=replace(payload, event=replace(runtime, payload=bounded)),
        ),
        True,
    )


__all__ = [
    "BoundedUIEventQueue",
    "EventPutFailureReason",
    "EventPutResult",
    "EventQueueStats",
]
