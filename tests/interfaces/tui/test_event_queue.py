from __future__ import annotations

import threading
import time

import pytest

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    AssistantContentDelta,
    AssistantStreamInterrupted,
    ErrorOccurred,
    OperationPhaseChanged,
    ProcessSessionChanged,
    RuntimeEvent,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
)
from reuleauxcoder.interfaces.events import (
    InteractionPromptPayload,
    RuntimeEventPayload,
    UIEvent,
    UIEventKind,
)
from reuleauxcoder.interfaces.interactions import ConfirmRequest
from reuleauxcoder.interfaces.tui.event_queue import (
    BoundedUIEventQueue,
    EventPutFailureReason,
)


def _runtime_event(
    payload,
    *,
    agent_id: str = "root",
    generation_owner_agent_id: str | None = None,
    turn_id: str = "turn-1",
    correlation_id: str | None = "attempt-1",
    session_generation: int = 1,
) -> UIEvent:
    runtime = RuntimeEvent(
        payload=payload,
        agent_id=agent_id,
        session_id="session",
        session_generation=session_generation,
        turn_id=turn_id,
        correlation_id=correlation_id,
    )
    return UIEvent.info(
        runtime.kind.value,
        kind=UIEventKind.AGENT,
        payload=RuntimeEventPayload(
            runtime,
            generation_owner_agent_id=generation_owner_agent_id,
        ),
    )


def _payload(event: UIEvent):
    assert isinstance(event.payload, RuntimeEventPayload)
    return event.payload.event.payload


def _control(index: int) -> UIEvent:
    return _runtime_event(
        TurnStarted(f"request {index}"),
        turn_id=f"control-{index}",
        correlation_id=None,
    )


def _process_change(
    *,
    stdout: str = "",
    stderr: str = "",
    output_truncated: bool = False,
    output_decode_replaced: bool = False,
) -> UIEvent:
    return _runtime_event(
        ProcessSessionChanged(
            change="published",
            process_session_id="process-1",
            state="running",
            stream_mode="pipe",
            backend="local",
            command="printf output",
            cwd="/tmp",
            elapsed_seconds=0.1,
            stdout=stdout,
            stderr=stderr,
            output_truncated=output_truncated,
            output_decode_replaced=output_decode_replaced,
        )
    )


def test_same_key_stream_events_coalesce_in_order() -> None:
    queue = BoundedUIEventQueue(capacity=8, control_reserve=2)

    first = queue.put(_runtime_event(AssistantContentDelta("hello ")))
    second = queue.put(_runtime_event(AssistantContentDelta("world")))

    assert first.wake_consumer is True
    assert second.coalesced is True
    events = queue.drain()
    assert len(events) == 1
    assert _payload(events[0]).text == "hello world"
    assert queue.stats().coalesced == 1


def test_control_event_is_a_coalescing_barrier() -> None:
    queue = BoundedUIEventQueue(capacity=8, control_reserve=2)
    queue.put(_runtime_event(AssistantContentDelta("before")))
    queue.put(
        _runtime_event(
            ToolCallStarted("tool-1", "shell", {}),
            correlation_id="tool-1",
        )
    )
    queue.put(_runtime_event(AssistantContentDelta("after")))

    events = queue.drain()

    assert len(events) == 3
    assert [_payload(event).text for event in (events[0], events[2])] == [
        "before",
        "after",
    ]


def test_capacity_reserve_bounds_transients_and_admits_control() -> None:
    queue = BoundedUIEventQueue(capacity=4, control_reserve=1)
    for index in range(4):
        result = queue.put(
            _runtime_event(
                AssistantContentDelta(str(index)),
                turn_id=f"turn-{index}",
            )
        )
        assert result.accepted is True

    stats = queue.stats()
    assert stats.depth == 3
    assert stats.transient_depth == 3
    assert stats.transient_dropped == 1

    assert queue.put(_control(1)).accepted is True
    stats = queue.stats()
    assert stats.depth == 4
    assert stats.high_watermark == 4
    assert stats.depth <= stats.capacity
    assert isinstance(_payload(queue.drain()[-1]), TurnStarted)


def test_control_evicts_transient_before_waiting() -> None:
    queue = BoundedUIEventQueue(capacity=3, control_reserve=1)
    queue.put(_runtime_event(AssistantContentDelta("lossy")))
    queue.put(_control(1))
    queue.put(_control(2))

    result = queue.put(_control(3), timeout=0.2)

    assert result.accepted is True
    assert queue.stats().must_deliver_waits == 0
    assert queue.stats().transient_dropped == 1
    assert all(isinstance(_payload(event), TurnStarted) for event in queue.drain())


def test_coalesced_text_has_a_hard_budget() -> None:
    queue = BoundedUIEventQueue(
        capacity=4,
        control_reserve=1,
        max_coalesced_chars=5,
    )
    queue.put(_runtime_event(AssistantContentDelta("abc")))
    queue.put(_runtime_event(AssistantContentDelta("def")))

    event = queue.drain()[0]

    assert _payload(event).text == "bcdef"
    assert queue.stats().coalesced == 1
    assert queue.stats().transient_dropped == 1


def test_same_process_deltas_coalesce_without_losing_output_or_flags() -> None:
    queue = BoundedUIEventQueue(capacity=4, control_reserve=1)

    queue.put(
        _process_change(
            stdout="first ",
            stderr="warning ",
            output_decode_replaced=True,
        )
    )
    result = queue.put(
        _process_change(
            stdout="second",
            stderr="error",
            output_truncated=True,
        )
    )

    payload = _payload(queue.drain()[0])
    assert result.coalesced is True
    assert payload.stdout == "first second"
    assert payload.stderr == "warning error"
    assert payload.output_truncated is True
    assert payload.output_decode_replaced is True


def test_process_delta_coalescing_is_bounded_and_marks_truncation() -> None:
    queue = BoundedUIEventQueue(
        capacity=4,
        control_reserve=1,
        max_coalesced_chars=5,
    )
    queue.put(_process_change(stdout="abc", stderr="123"))
    queue.put(_process_change(stdout="def", stderr="456"))

    payload = _payload(queue.drain()[0])
    assert payload.stdout == "bcdef"
    assert payload.stderr == "23456"
    assert payload.output_truncated is True
    assert queue.stats().transient_dropped == 1


def test_single_process_delta_budget_marks_truncation() -> None:
    queue = BoundedUIEventQueue(
        capacity=4,
        control_reserve=1,
        max_coalesced_chars=5,
    )

    queue.put(_process_change(stdout="abcdef"))

    payload = _payload(queue.drain()[0])
    assert payload.stdout == "bcdef"
    assert payload.output_truncated is True


@pytest.mark.parametrize(
    "event",
    [
        _runtime_event(TurnFinished("done", render_response=False)),
        _runtime_event(
            ToolCallFinished("tool-1", "shell", ToolOutcome(content="done"))
        ),
        _runtime_event(ApprovalRequested("approval-1", "Approve?")),
        _runtime_event(AssistantStreamInterrupted("attempt-1", 1)),
        _runtime_event(ErrorOccurred("failed")),
        _runtime_event(
            OperationPhaseChanged("op-1", "test", "done", status="completed")
        ),
        _runtime_event(
            ProcessSessionChanged(
                change="completed",
                process_session_id="process-1",
                state="exited",
                stream_mode="pipe",
                backend="local",
                command="true",
                cwd="/tmp",
                elapsed_seconds=0.1,
            )
        ),
        UIEvent.info(
            "Confirm",
            kind=UIEventKind.APPROVAL,
            payload=InteractionPromptPayload(
                ConfirmRequest(title="Confirm", message="Continue?")
            ),
        ),
    ],
)
def test_terminal_and_control_payloads_use_reserved_delivery(event: UIEvent) -> None:
    queue = BoundedUIEventQueue(capacity=2, control_reserve=1)
    queue.put(_runtime_event(AssistantContentDelta("transient")))

    assert queue.put(event).accepted is True

    stats = queue.stats()
    assert stats.depth == 2
    assert stats.transient_depth == 1


def test_must_deliver_wait_succeeds_after_consumer_drains() -> None:
    queue = BoundedUIEventQueue(capacity=2, control_reserve=1)
    queue.put(_control(1))
    queue.put(_control(2))
    result = []

    producer = threading.Thread(
        target=lambda: result.append(queue.put(_control(3), timeout=1.0))
    )
    producer.start()
    _wait_until(lambda: queue.stats().must_deliver_waits == 1)

    first_batch = queue.drain()
    producer.join(timeout=1.0)

    assert not producer.is_alive()
    assert result[0].accepted is True
    assert [_payload(event).user_input for event in first_batch] == [
        "request 1",
        "request 2",
    ]
    assert _payload(queue.drain()[0]).user_input == "request 3"
    assert queue.stats().must_deliver_timeouts == 0


def test_must_deliver_timeout_is_bounded_and_observable() -> None:
    queue = BoundedUIEventQueue(capacity=2, control_reserve=1)
    queue.put(_control(1))
    queue.put(_control(2))

    started_at = time.monotonic()
    result = queue.put(_control(3), timeout=0.05)
    elapsed = time.monotonic() - started_at

    assert result.accepted is False
    assert result.reason is EventPutFailureReason.CONTROL_TIMEOUT
    assert 0.04 <= elapsed < 0.3
    assert queue.stats().must_deliver_waits == 1
    assert queue.stats().must_deliver_timeouts == 1
    assert len(queue.drain()) == 2


def test_close_wakes_blocked_publishers_and_preserves_pending_events() -> None:
    queue = BoundedUIEventQueue(capacity=2, control_reserve=1)
    queue.put(_control(1))
    queue.put(_control(2))
    result = []
    producer = threading.Thread(
        target=lambda: result.append(queue.put(_control(3), timeout=5.0))
    )
    producer.start()
    _wait_until(lambda: queue.stats().must_deliver_waits == 1)

    queue.close()
    queue.close()
    producer.join(timeout=0.5)

    assert not producer.is_alive()
    assert result[0].accepted is False
    assert result[0].reason is EventPutFailureReason.CLOSED
    after_close = queue.put(_control(4))
    assert after_close.accepted is False
    assert after_close.reason is EventPutFailureReason.CLOSED
    stats = queue.stats()
    assert stats.closed is True
    assert stats.must_deliver_timeouts == 0
    assert stats.closed_dropped == 2
    assert len(queue.drain()) == 2


def test_transient_rejection_reports_capacity_reason() -> None:
    queue = BoundedUIEventQueue(capacity=2, control_reserve=1)
    queue.put(_control(1))
    queue.put(_control(2))

    result = queue.put(_runtime_event(AssistantContentDelta("late")))

    assert result.accepted is False
    assert result.reason is EventPutFailureReason.TRANSIENT_CAPACITY


def test_generation_floor_discards_queued_and_late_stale_runtime_events() -> None:
    queue = BoundedUIEventQueue(capacity=4, control_reserve=1)
    queue.put(_runtime_event(TurnStarted("old"), session_generation=1))
    queue.put(UIEvent.info("unrouted"))

    assert queue.advance_generation(2) == 1
    stale = queue.put(_runtime_event(TurnStarted("late-old"), session_generation=1))
    current = queue.put(_runtime_event(TurnStarted("current"), session_generation=2))

    assert stale.accepted is False
    assert stale.reason is EventPutFailureReason.STALE_GENERATION
    assert current.accepted is True
    assert queue.stats().stale_generation_dropped == 2
    assert [event.message for event in queue.drain()] == ["unrouted", "turn_started"]


def test_generation_floor_rejects_event_drained_just_before_advance() -> None:
    queue = BoundedUIEventQueue(capacity=4, control_reserve=1)
    queue.put(_runtime_event(TurnStarted("old"), session_generation=1))
    drained = queue.drain()

    queue.advance_generation(2)

    assert queue.reject_stale(drained[0]) is True
    assert queue.stats().stale_generation_dropped == 1


def test_root_generation_floor_does_not_drop_independent_peer_events() -> None:
    queue = BoundedUIEventQueue(
        capacity=4,
        control_reserve=1,
        generation_agent_id="root",
    )
    queue.put(_runtime_event(TurnStarted("old-root"), session_generation=1))
    queue.put(
        _runtime_event(
            TurnStarted("current-peer"),
            agent_id="peer",
            session_generation=1,
        )
    )

    assert queue.advance_generation(2) == 1
    late_peer = queue.put(
        _runtime_event(
            TurnStarted("late-peer"),
            agent_id="peer",
            session_generation=1,
        )
    )

    assert late_peer.accepted is True
    assert [event.payload.event.agent_id for event in queue.drain()] == [
        "peer",
        "peer",
    ]


def test_root_generation_floor_drops_old_child_owned_by_root() -> None:
    queue = BoundedUIEventQueue(
        capacity=4,
        control_reserve=1,
        generation_agent_id="root",
    )
    queue.put(
        _runtime_event(
            TurnStarted("old-child"),
            agent_id="sa_child",
            generation_owner_agent_id="root",
            session_generation=1,
        )
    )

    assert queue.advance_generation(2) == 1
    late_child = queue.put(
        _runtime_event(
            TurnStarted("late-child"),
            agent_id="sa_child",
            generation_owner_agent_id="root",
            session_generation=1,
        )
    )

    assert late_child.reason is EventPutFailureReason.STALE_GENERATION
    assert queue.drain() == []


def test_coalescing_preserves_child_generation_owner() -> None:
    queue = BoundedUIEventQueue(
        capacity=4,
        control_reserve=1,
        generation_agent_id="root",
    )
    for text in ("first", "second"):
        queue.put(
            _runtime_event(
                AssistantContentDelta(text),
                agent_id="sa_child",
                generation_owner_agent_id="root",
                session_generation=1,
            )
        )

    assert queue.stats().coalesced == 1
    assert queue.advance_generation(2) == 1
    assert queue.drain() == []


def test_concurrent_transient_producers_remain_bounded_and_controls_arrive() -> None:
    queue = BoundedUIEventQueue(capacity=8, control_reserve=2)
    start = threading.Barrier(5)
    failures = []

    def produce(index: int) -> None:
        try:
            start.wait()
            for _ in range(1_000):
                queue.put(
                    _runtime_event(
                        AssistantContentDelta(str(index)),
                        turn_id=f"producer-{index}",
                    )
                )
        except BaseException as error:
            failures.append(error)

    producers = [threading.Thread(target=produce, args=(index,)) for index in range(4)]
    for producer in producers:
        producer.start()
    start.wait()
    for producer in producers:
        producer.join(timeout=2.0)

    assert not failures
    assert all(not producer.is_alive() for producer in producers)
    assert queue.stats().depth <= queue.stats().capacity

    for index in range(4):
        assert queue.put(
            _runtime_event(
                TurnFinished(f"final {index}", render_response=False),
                turn_id=f"producer-{index}",
            )
        ).accepted

    events = queue.drain()
    completions = [
        _payload(event) for event in events if isinstance(_payload(event), TurnFinished)
    ]
    assert [payload.response for payload in completions] == [
        "final 0",
        "final 1",
        "final 2",
        "final 3",
    ]
    assert queue.stats().high_watermark <= queue.stats().capacity


def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")
