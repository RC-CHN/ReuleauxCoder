from __future__ import annotations

from reuleauxcoder.domain.runtime.events import (
    AssistantContentDelta,
    RuntimeEvent,
    TurnFinished,
)
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.interfaces.events import (
    RuntimeEventPayload,
    UIEvent,
    UIEventKind,
)
from reuleauxcoder.interfaces.tui.event_adapter import MiniTUIEventAdapter
from reuleauxcoder.presentation import AssistantCell


def _event(payload, *, turn_id: str) -> UIEvent:
    runtime = RuntimeEvent(
        payload=payload,
        agent_id="root",
        session_id="session",
        session_generation=1,
        turn_id=turn_id,
        correlation_id=f"attempt:{turn_id}",
    )
    return UIEvent.info(
        runtime.kind.value,
        kind=UIEventKind.AGENT,
        payload=RuntimeEventPayload(runtime),
    )


def test_adapter_invalidates_once_per_pending_burst() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    invalidations = []
    adapter.bind_invalidator(lambda: invalidations.append(True))

    for _ in range(100):
        adapter.on_ui_event(_event(AssistantContentDelta("x"), turn_id="turn-1"))

    assert len(invalidations) == 1
    assert adapter.event_queue_stats().depth == 1
    adapter.transcript_layout()

    adapter.on_ui_event(_event(AssistantContentDelta("y"), turn_id="turn-1"))

    assert len(invalidations) == 2


def test_terminal_response_recovers_an_evicted_assistant_stream() -> None:
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        event_queue_capacity=3,
        event_queue_control_reserve=1,
    )
    adapter.on_ui_event(
        _event(AssistantContentDelta("partial"), turn_id="target")
    )
    adapter.on_ui_event(_event(AssistantContentDelta("other"), turn_id="other-1"))
    adapter.on_ui_event(_event(AssistantContentDelta("newer"), turn_id="other-2"))
    assert adapter.event_queue_stats().transient_dropped == 1

    adapter.on_ui_event(
        _event(
            TurnFinished("authoritative final", render_response=False),
            turn_id="target",
        )
    )
    adapter.transcript_layout()

    target = next(
        cell
        for cell in adapter.transcript.state.transcript.cells
        if isinstance(cell, AssistantCell) and cell.id.endswith(":target")
    )
    assert target.text == "authoritative final"
    assert target.complete is True
    assert adapter.event_queue_stats().depth == 0


def test_ten_thousand_delta_burst_stays_compact_and_finishes_exactly() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    invalidations = []
    adapter.bind_invalidator(lambda: invalidations.append(True))
    expected = "x" * 10_000

    for _ in range(10_000):
        adapter.on_ui_event(_event(AssistantContentDelta("x"), turn_id="burst"))
    adapter.on_ui_event(
        _event(TurnFinished(expected, render_response=False), turn_id="burst")
    )

    stats = adapter.event_queue_stats()
    assert stats.depth == 2
    assert stats.high_watermark == 2
    assert stats.coalesced == 9_999
    assert len(invalidations) == 1

    adapter.transcript_layout()
    cell = next(
        cell
        for cell in adapter.transcript.state.transcript.cells
        if isinstance(cell, AssistantCell) and cell.id.endswith(":burst")
    )
    assert cell.text == expected
    assert cell.complete is True


def test_adapter_records_content_free_queue_pressure_metrics() -> None:
    monitor = RuntimePerformanceMonitor()
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        performance_monitor=monitor,
        event_queue_capacity=4,
        event_queue_control_reserve=1,
    )
    adapter.on_ui_event(_event(AssistantContentDelta("secret-a"), turn_id="turn"))
    adapter.on_ui_event(_event(AssistantContentDelta("secret-b"), turn_id="turn"))

    adapter.transcript_layout()

    samples = monitor.snapshot(category="ui_queue")
    assert len(samples) == 1
    sample = samples[0]
    assert sample.name == "drain"
    attributes = sample.attribute_map()
    assert attributes["batch_size"] == 1
    assert attributes["high_watermark"] == 1
    assert attributes["coalesced"] == 1
    assert attributes["transient_dropped"] == 0
    assert "secret" not in repr(attributes)

    adapter.on_ui_event(_event(AssistantContentDelta("next"), turn_id="turn"))
    adapter.transcript_layout()
    assert len(monitor.snapshot(category="ui_queue")) == 1
