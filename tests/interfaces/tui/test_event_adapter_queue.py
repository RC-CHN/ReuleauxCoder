from __future__ import annotations

from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.runtime.events import (
    AssistantContentDelta,
    RuntimeEvent,
    TurnStarted,
    TurnFinished,
)
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.app.commands.view_models import HelpViewModel
from reuleauxcoder.interfaces.events import (
    RuntimeEventPayload,
    UIEvent,
    UIEventKind,
    ViewEventPayload,
)
from reuleauxcoder.interfaces.tui.event_adapter import MiniTUIEventAdapter
from reuleauxcoder.interfaces.tui.event_queue import EventPutFailureReason
from reuleauxcoder.presentation import AssistantCell


def _event(payload, *, turn_id: str, session_generation: int = 1) -> UIEvent:
    runtime = RuntimeEvent(
        payload=payload,
        agent_id="root",
        session_id="session",
        session_generation=session_generation,
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


def test_control_timeout_returns_rejection_and_surfaces_bounded_incident() -> None:
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        event_queue_capacity=2,
        event_queue_control_reserve=1,
        event_queue_must_deliver_timeout=0.01,
    )
    adapter.on_ui_event(_event(TurnStarted("first"), turn_id="first"))
    adapter.on_ui_event(_event(TurnStarted("second"), turn_id="second"))

    result = adapter.on_ui_event(
        _event(TurnStarted("rejected-secret"), turn_id="third")
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert result.accepted is False
    assert result.reason is EventPutFailureReason.CONTROL_TIMEOUT
    assert "UI delivery failed (control_timeout)" in rendered
    assert "rejected-secret" not in rendered


def test_delivery_and_projection_incidents_reach_agent_sink_without_content() -> None:
    incidents = []
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        incident_sink=lambda phase, error_type, ref, count, **_route: incidents.append(
            (phase, error_type, ref, count)
        ),
    )
    adapter.execution.apply = lambda _runtime: (_ for _ in ()).throw(
        GeneratorExit("projection-secret")
    )

    adapter.on_ui_event(_event(TurnStarted("event-secret"), turn_id="turn"))
    adapter.transcript_layout()
    adapter.close()
    result = adapter.on_ui_event(
        _event(TurnStarted("delivery-secret"), turn_id="closed")
    )

    assert result.reason is EventPutFailureReason.CLOSED
    assert incidents == [
        ("ui_projection", "GeneratorExit", "execution", 1),
        ("ui_delivery", "EventRejected", "closed", 1),
    ]
    assert "secret" not in repr(incidents)


def test_projection_incident_carries_root_generation_route() -> None:
    incidents = []
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        session_generation=3,
        incident_sink=lambda phase, error_type, ref, count, **route: incidents.append(
            (phase, error_type, ref, count, route)
        ),
    )
    adapter.execution.apply = lambda _runtime: (_ for _ in ()).throw(
        SystemExit("projection-secret")
    )

    adapter.on_ui_event(
        _event(TurnStarted("event-secret"), turn_id="turn", session_generation=3)
    )
    adapter.transcript_layout()

    assert incidents == [
        (
            "ui_projection",
            "SystemExit",
            "execution",
            1,
            {"agent_id": "root", "session_generation": 3},
        )
    ]


def test_peer_projection_incident_uses_current_root_generation() -> None:
    incidents = []
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        session_generation=3,
        incident_sink=lambda phase, error_type, ref, count, **route: incidents.append(
            (phase, error_type, ref, count, route)
        ),
    )
    adapter.execution.apply = lambda _runtime: (_ for _ in ()).throw(
        SystemExit("projection-secret")
    )

    adapter.on_ui_event(
        UIEvent.info(
            "runtime",
            payload=RuntimeEventPayload(
                RuntimeEvent(
                    payload=TurnStarted("peer-event-secret"),
                    agent_id="peer",
                    session_generation=1,
                    turn_id="turn",
                )
            ),
        )
    )
    adapter.transcript_layout()

    assert incidents == [
        (
            "ui_projection",
            "SystemExit",
            "execution",
            1,
            {"agent_id": "root", "session_generation": 3},
        )
    ]


def test_real_ui_failure_reaches_agent_next_request_without_content() -> None:
    agent = Agent(llm=SimpleNamespace(model="test-model"), tools=[])
    adapter = MiniTUIEventAdapter(
        root_agent_id=agent.agent_id,
        session_generation=agent.session_generation,
        incident_sink=agent.record_runtime_issue,
    )
    adapter.execution.apply = lambda _runtime: (_ for _ in ()).throw(
        GeneratorExit("projection-secret")
    )

    adapter.on_ui_event(
        UIEvent.info(
            "runtime",
            kind=UIEventKind.AGENT,
            payload=RuntimeEventPayload(
                RuntimeEvent(
                    payload=TurnStarted("event-secret"),
                    agent_id=agent.agent_id,
                    session_generation=agent.session_generation,
                    turn_id="turn",
                )
            ),
        )
    )
    adapter.transcript_layout()
    content = agent._loop._full_messages()[-1]["content"]

    assert '"runtime_incidents":{"status":"degraded"' in content
    assert '"phase":"ui_projection"' in content
    assert '"error_type":"GeneratorExit"' in content
    assert '"ref":"execution"' in content
    assert "projection-secret" not in content
    assert "event-secret" not in content


def test_generation_advance_drops_queued_events_and_unpainted_incidents() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root", session_generation=1)
    adapter.bind_invalidator(
        lambda: (_ for _ in ()).throw(SystemExit("old-invalidate-secret"))
    )
    adapter.on_ui_event(
        _event(TurnStarted("old-event-secret"), turn_id="old", session_generation=1)
    )
    adapter.bind_invalidator(lambda: None)
    plan = SimpleNamespace(revision=0, session_generation=2, items=(), explanation=None)
    progress = SimpleNamespace(revision=0, phase=None, summary=None, next=None)

    adapter.restore_control_state(plan, progress, session_id="session-2")
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    stats = adapter.event_queue_stats()
    assert stats.stale_generation_dropped == 1
    assert adapter.stale_incident_dropped == 1
    assert "old-event-secret" not in rendered
    assert "UI refresh request failed" not in rendered


def test_late_old_generation_event_is_metrics_only() -> None:
    incidents = []
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        session_generation=1,
        incident_sink=lambda *_facts, **route: incidents.append(route),
    )
    plan = SimpleNamespace(revision=0, session_generation=2, items=(), explanation=None)
    progress = SimpleNamespace(revision=0, phase=None, summary=None, next=None)
    adapter.restore_control_state(plan, progress, session_id="session-2")

    result = adapter.on_ui_event(
        _event(TurnStarted("late-secret"), turn_id="late", session_generation=1)
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert result.reason is EventPutFailureReason.STALE_GENERATION
    assert adapter.event_queue_stats().stale_generation_dropped == 1
    assert incidents == []
    assert "late-secret" not in rendered
    assert "UI delivery failed" not in rendered


def test_incident_sink_failure_preserves_primary_result_and_stays_local() -> None:
    def broken_sink(*_facts, **_route) -> None:
        raise SystemExit("observer-secret")

    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        incident_sink=broken_sink,
    )
    adapter.close()

    result = adapter.on_ui_event(
        _event(TurnStarted("delivery-secret"), turn_id="closed")
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert result.reason is EventPutFailureReason.CLOSED
    assert "UI delivery failed (closed)" in rendered
    assert "UI incident reporting failed (SystemExit)" in rendered
    assert "observer-secret" not in rendered
    assert "delivery-secret" not in rendered


def test_keyboard_interrupt_from_incident_sink_propagates() -> None:
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        incident_sink=lambda *_facts, **_route: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )
    adapter.close()

    with pytest.raises(KeyboardInterrupt):
        adapter.on_ui_event(_event(TurnStarted("interrupt"), turn_id="closed"))


def test_closed_delivery_incidents_are_bounded_and_visible_without_content() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.close()

    results = [
        adapter.on_ui_event(
            _event(TurnStarted(f"closed-secret-{index}"), turn_id=str(index))
        )
        for index in range(32)
    ]
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert all(result.reason is EventPutFailureReason.CLOSED for result in results)
    assert rendered.count("UI delivery failed (closed)") == 1
    assert "[count=32]" in rendered
    assert "closed-secret" not in rendered


def test_local_incident_count_saturates() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.close()
    adapter.on_ui_event(_event(TurnStarted("closed"), turn_id="first"))
    incident = next(iter(adapter._pending_incidents))
    adapter._pending_incidents[incident] = 1_000_000

    adapter.on_ui_event(_event(TurnStarted("closed"), turn_id="second"))
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert "[count=1000000]" in rendered


def test_incident_keys_are_deterministically_bounded_with_overflow() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.close()

    for index in range(10):
        error_type = type(f"InvalidateFailure{index}", (SystemExit,), {})

        def invalidate(error_type=error_type) -> None:
            raise error_type()

        adapter.bind_invalidator(invalidate)
        adapter.on_ui_event(_event(TurnStarted("closed"), turn_id=str(index)))

    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert rendered.count("UI delivery failed (closed)") == 1
    assert (
        "UI delivery failed (closed); event projection was rejected. [count=10]"
        in rendered
    )
    assert "InvalidateFailure0" in rendered
    assert "InvalidateFailure6" in rendered
    assert "InvalidateFailure7" not in rendered
    assert "Additional UI incidents were suppressed. [count=3]" in rendered


def test_transient_capacity_rejection_is_metrics_only() -> None:
    monitor = RuntimePerformanceMonitor()
    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        performance_monitor=monitor,
        event_queue_capacity=2,
        event_queue_control_reserve=1,
    )
    adapter.on_ui_event(_event(TurnStarted("first"), turn_id="first"))
    adapter.on_ui_event(_event(TurnStarted("second"), turn_id="second"))

    result = adapter.on_ui_event(
        _event(AssistantContentDelta("transient-secret"), turn_id="third")
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())
    enqueue = monitor.snapshot(category="ui_queue")[0]

    assert result.reason is EventPutFailureReason.TRANSIENT_CAPACITY
    assert enqueue.attribute_map()["failure_reason"] == "transient_capacity"
    assert "UI delivery failed" not in rendered
    assert "transient-secret" not in rendered


def test_runtime_handler_failure_does_not_block_transcript_projection() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.runtime_event_handler = lambda _runtime: (_ for _ in ()).throw(
        SystemExit("handler-secret")
    )

    result = adapter.on_ui_event(
        _event(AssistantContentDelta("still visible"), turn_id="turn")
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert result.accepted is True
    assert "still visible" in rendered
    assert "UI projection failed in runtime_handler (SystemExit)" in rendered
    assert "handler-secret" not in rendered


def test_transcript_failure_does_not_block_execution_projection() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    applied = []
    adapter.execution.apply = lambda runtime: applied.append(runtime)
    adapter.transcript.apply = lambda _runtime: (_ for _ in ()).throw(
        GeneratorExit("transcript-secret")
    )

    adapter.on_ui_event(_event(TurnStarted("run"), turn_id="turn"))
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert len(applied) == 1
    assert "UI projection failed in transcript (GeneratorExit)" in rendered
    assert "transcript-secret" not in rendered


def test_interactive_failure_falls_back_to_passive_projection() -> None:
    adapter = MiniTUIEventAdapter()
    adapter.interactive_view_handler = lambda _payload: (_ for _ in ()).throw(
        SystemExit("interactive-secret")
    )
    event = UIEvent.info(
        "Help",
        kind=UIEventKind.VIEW,
        payload=ViewEventPayload(
            action="open",
            view_type="help",
            title="Help",
            view_model=HelpViewModel(sections=()),
        ),
    )

    adapter.on_ui_event(event)
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert "(no commands available)" in rendered
    assert "UI projection failed in interactive (SystemExit)" in rendered
    assert "interactive-secret" not in rendered


def test_invalidator_failure_preserves_accepted_result_and_becomes_visible() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.bind_invalidator(
        lambda: (_ for _ in ()).throw(SystemExit("invalidate-secret"))
    )

    result = adapter.on_ui_event(
        _event(AssistantContentDelta("accepted"), turn_id="turn")
    )
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert result.accepted is True
    assert "accepted" in rendered
    assert "UI refresh request failed (SystemExit)" in rendered
    assert "invalidate-secret" not in rendered


def test_non_event_invalidator_failures_preserve_completed_updates() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.bind_invalidator(
        lambda: (_ for _ in ()).throw(GeneratorExit("invalidate-secret"))
    )
    plan = SimpleNamespace(revision=0, session_generation=2, items=(), explanation=None)
    progress = SimpleNamespace(revision=0, phase=None, summary=None, next=None)

    adapter.append_user_command("/help")
    adapter.restore_control_state(plan, progress, session_id="session-2")
    adapter.clear_transcript()
    adapter.append_restored_conversation([{"role": "assistant", "content": "restored"}])
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert "restored" in rendered
    assert "UI refresh request failed (GeneratorExit)" in rendered
    assert "[count=4]" in rendered
    assert "invalidate-secret" not in rendered


def test_keyboard_interrupt_from_non_event_invalidator_propagates() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.bind_invalidator(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        adapter.append_user_command("/help")

    assert adapter.transcript.state.transcript.cells


def test_monitor_failure_preserves_delivery_and_surfaces_on_next_paint() -> None:
    class BrokenMonitor:
        def record(self, *args, **kwargs) -> None:  # noqa: ARG002
            raise GeneratorExit("monitor-secret")

    adapter = MiniTUIEventAdapter(
        root_agent_id="root",
        performance_monitor=BrokenMonitor(),
    )
    invalidations = []
    adapter.bind_invalidator(lambda: invalidations.append(True))

    result = adapter.on_ui_event(
        _event(AssistantContentDelta("accepted"), turn_id="turn")
    )
    invalidations_before_drain = len(invalidations)
    adapter.transcript_layout()
    assert len(invalidations) > invalidations_before_drain
    rendered = "".join(text for _style, text in adapter.transcript_fragments())

    assert result.accepted is True
    assert "UI performance monitoring failed (GeneratorExit)" in rendered
    assert "monitor-secret" not in rendered


def test_incident_projection_failure_does_not_recurse() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.execution.apply = lambda _runtime: (_ for _ in ()).throw(
        GeneratorExit("projection-secret")
    )
    adapter.transcript.append_notice = lambda **_kwargs: (_ for _ in ()).throw(
        SystemExit("incident-secret")
    )

    adapter.on_ui_event(_event(TurnStarted("continue"), turn_id="turn"))

    adapter.transcript_layout()


def test_incident_carrier_base_exception_does_not_recurse() -> None:
    class BrokenCarrier:
        def get(self, _incident):
            raise SystemExit("carrier-secret")

    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter._pending_incidents = BrokenCarrier()
    adapter.execution.apply = lambda _runtime: (_ for _ in ()).throw(
        GeneratorExit("projection-secret")
    )

    adapter.on_ui_event(_event(TurnStarted("continue"), turn_id="turn"))

    adapter.transcript_layout()


def test_keyboard_interrupt_from_secondary_handler_propagates() -> None:
    adapter = MiniTUIEventAdapter(root_agent_id="root")
    adapter.runtime_event_handler = lambda _runtime: (_ for _ in ()).throw(
        KeyboardInterrupt()
    )
    adapter.on_ui_event(_event(TurnStarted("interrupt"), turn_id="turn"))

    with pytest.raises(KeyboardInterrupt):
        adapter.transcript_layout()
