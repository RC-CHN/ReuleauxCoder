import queue

import pytest

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.runtime.events import (
    ErrorOccurred,
    OperationPhaseChanged,
    RuntimeEvent,
    ToolCallFinished,
    ToolCallStarted,
)
from reuleauxcoder.interfaces.events import (
    AgentEventBridge,
    UIEvent,
    UIEventBus,
    UIEventKind,
    UIEventLevel,
    RuntimeEventPayload,
    ViewEventPayload,
)


def test_ui_event_factory_methods_set_level_and_kind() -> None:
    assert UIEvent.info("x", kind=UIEventKind.COMMAND).level is UIEventLevel.INFO
    assert UIEvent.success("x").level is UIEventLevel.SUCCESS
    assert UIEvent.warning("x").level is UIEventLevel.WARNING
    assert UIEvent.error("x").level is UIEventLevel.ERROR
    assert UIEvent.debug("x").level is UIEventLevel.DEBUG


def test_ui_event_bus_replays_history_to_new_subscriber() -> None:
    bus = UIEventBus()
    seen = []

    bus.info("first")
    bus.subscribe(lambda event: seen.append(event.message), replay_history=True)

    assert seen == ["first"]


def test_ui_event_bus_exposes_immutable_history_snapshot() -> None:
    bus = UIEventBus()
    bus.info("first")

    snapshot = bus.history_snapshot()
    bus.info("second")

    assert tuple(event.message for event in snapshot) == ("first",)
    assert tuple(event.message for event in bus.history_snapshot()) == (
        "first",
        "second",
    )


def test_ui_event_bus_bounds_replay_history() -> None:
    bus = UIEventBus(max_history=3)

    for index in range(5):
        bus.info(f"event-{index}")

    assert tuple(event.message for event in bus.history_snapshot()) == (
        "event-2",
        "event-3",
        "event-4",
    )


def test_ui_event_bus_rejects_non_positive_history_limit() -> None:
    try:
        UIEventBus(max_history=0)
    except ValueError as error:
        assert str(error) == "max_history must be positive"
    else:
        raise AssertionError("non-positive history limit must be rejected")


def test_running_operation_phases_are_delivered_but_not_replayed() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    bus.emit_operation_phase(
        operation_id="request-1",
        operation="model",
        phase="connect",
        started_at=10.0,
        cancelable=True,
    )

    assert len(seen) == 1
    assert isinstance(seen[0].payload, RuntimeEventPayload)
    assert isinstance(seen[0].payload.event.payload, OperationPhaseChanged)
    assert bus.history_snapshot() == ()


def test_terminal_operation_phases_are_replayed() -> None:
    bus = UIEventBus()

    bus.emit_operation_phase(
        operation_id="request-1",
        operation="model",
        phase="done",
        status="completed",
    )

    history = bus.history_snapshot()
    assert len(history) == 1
    assert isinstance(history[0].payload, RuntimeEventPayload)
    assert history[0].payload.event.payload.status == "completed"


def test_ui_event_bus_emit_ignores_handler_exceptions() -> None:
    bus = UIEventBus()
    seen = []

    def broken_handler(event):
        raise RuntimeError("boom")

    def good_handler(event):
        seen.append(event.message)

    bus.subscribe(broken_handler, replay_history=False)
    bus.subscribe(good_handler, replay_history=False)
    bus.info("hello")

    assert seen == ["hello"]


def test_ui_event_bus_reports_base_exception_without_blocking_dispatch() -> None:
    incidents = []
    seen = []
    bus = UIEventBus()
    bus.bind_subscriber_failure_sink(
        lambda phase, error_type, ref, count, **_route: incidents.append(
            (phase, error_type, ref, count)
        )
    )

    def broken_handler(_event) -> None:
        raise SystemExit("subscriber-secret")

    bus.subscribe(broken_handler, replay_history=False)
    bus.subscribe(lambda event: seen.append(event.message), replay_history=False)

    bus.info("hello")

    assert seen == ["hello"]
    assert incidents == [("ui_subscriber", "SystemExit", "dispatch", 1)]
    assert "subscriber-secret" not in repr(incidents)


def test_ui_event_bus_reports_replay_and_queued_drain_failures() -> None:
    incidents = []
    event_queue: queue.Queue = queue.Queue()
    bus = UIEventBus(
        event_queue=event_queue,
        subscriber_failure_sink=lambda phase, error_type, ref, count, **_route: (
            incidents.append((phase, error_type, ref, count))
        ),
    )
    bus.info("history")

    def broken_handler(_event) -> None:
        raise GeneratorExit("subscriber-secret")

    bus.subscribe(broken_handler, replay_history=True)
    bus.drain()

    assert incidents == [
        ("ui_subscriber", "GeneratorExit", "history_replay", 1),
        ("ui_subscriber", "GeneratorExit", "dispatch", 1),
    ]


def test_ui_event_bus_secondary_failure_does_not_cover_delivery() -> None:
    seen = []

    def broken_sink(*_facts, **_route) -> None:
        raise GeneratorExit("observer-secret")

    bus = UIEventBus(subscriber_failure_sink=broken_sink)
    bus.subscribe(
        lambda _event: (_ for _ in ()).throw(RuntimeError("primary-secret")),
        replay_history=False,
    )
    bus.subscribe(lambda event: seen.append(event.message), replay_history=False)

    bus.info("still-delivered")

    assert seen == ["still-delivered"]
    pending = bus.subscriber_failure_snapshot()
    assert [fact.error_type for fact in pending] == ["RuntimeError", "GeneratorExit"]
    assert "secret" not in repr(pending)

    recovered = []
    bus.bind_subscriber_failure_sink(
        lambda phase, error_type, ref, count, **route: recovered.append(
            (phase, error_type, ref, count, route)
        )
    )
    assert [fact[1] for fact in recovered] == ["RuntimeError", "GeneratorExit"]
    assert bus.subscriber_failure_snapshot() == ()


def test_ui_event_bus_routes_runtime_failures_without_default_misattribution() -> None:
    root_incidents = []
    peer_incidents = []
    late_incidents = []
    bus = UIEventBus()

    def collect(target):
        return lambda phase, error_type, ref, count, **route: target.append(
            (phase, error_type, ref, count, route)
        )

    bus.bind_subscriber_failure_sink(
        collect(root_incidents), agent_id="root", default=True
    )
    bus.bind_subscriber_failure_sink(collect(peer_incidents), agent_id="peer")
    bus.subscribe(
        lambda _event: (_ for _ in ()).throw(SystemExit("subscriber-secret")),
        replay_history=False,
    )

    bus.emit_runtime(
        RuntimeEvent(
            payload=ErrorOccurred("peer-event-secret"),
            agent_id="peer",
            session_generation=4,
        )
    )
    bus.emit_runtime(
        RuntimeEvent(
            payload=ErrorOccurred("late-event-secret"),
            agent_id="late-peer",
            session_generation=7,
        )
    )
    bus.info("unrouted")

    assert len(peer_incidents) == 1
    assert peer_incidents[0][-1] == {
        "agent_id": "peer",
        "session_generation": 4,
    }
    assert len(root_incidents) == 1
    assert root_incidents[0][-1] == {
        "agent_id": None,
        "session_generation": None,
    }
    pending = bus.subscriber_failure_snapshot()
    assert len(pending) == 1
    assert pending[0].agent_id == "late-peer"
    assert "secret" not in repr(peer_incidents + root_incidents + list(pending))

    bus.bind_subscriber_failure_sink(collect(late_incidents), agent_id="late-peer")
    assert len(late_incidents) == 1
    assert bus.subscriber_failure_snapshot() == ()


def test_ui_event_bus_discards_route_rejected_stale_failure_as_metric() -> None:
    bus = UIEventBus()
    bus.bind_subscriber_failure_sink(
        lambda *_facts, **_route: False,
        agent_id="root",
    )
    bus.subscribe(
        lambda _event: (_ for _ in ()).throw(RuntimeError("stale-secret")),
        replay_history=False,
    )

    bus.emit_runtime(
        RuntimeEvent(
            payload=ErrorOccurred("event-secret"),
            agent_id="root",
            session_generation=1,
        )
    )

    assert bus.subscriber_failure_stale_dropped == 1
    assert bus.subscriber_failure_snapshot() == ()


def test_ui_event_bus_keyboard_interrupt_propagates() -> None:
    bus = UIEventBus()
    bus.subscribe(
        lambda _event: (_ for _ in ()).throw(KeyboardInterrupt()),
        replay_history=False,
    )

    with pytest.raises(KeyboardInterrupt):
        bus.info("interrupt")


def test_ui_event_bus_open_view_emits_structured_view_event() -> None:
    from reuleauxcoder.app.commands.view_models import HelpViewModel

    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    bus.open_view(
        "help",
        title="Help",
        view_model=HelpViewModel(sections=()),
        focus=False,
        reuse_key="help",
    )

    event = seen[0]
    assert event.kind is UIEventKind.VIEW
    assert isinstance(event.payload, ViewEventPayload)
    assert event.payload.action == "open"
    assert event.payload.view_type == "help"
    assert event.payload.title == "Help"
    assert event.payload.view_model.view_type == "help"
    assert event.payload.focus is False
    assert event.payload.reuse_key == "help"


def test_agent_event_bridge_maps_error_to_error_level() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(AgentEvent.error("boom"))

    event = seen[0]
    assert event.kind is UIEventKind.AGENT
    assert event.level is UIEventLevel.ERROR
    assert isinstance(event.payload, RuntimeEventPayload)
    assert isinstance(event.payload.event.payload, ErrorOccurred)
    assert event.payload.event.payload.message == "boom"


def test_agent_event_bridge_maps_tool_events_to_debug_level() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(
        AgentEvent.tool_call_start(
            "shell", {"command": "ls"}, tool_call_id="call-start"
        )
    )

    event = seen[0]
    assert event.kind is UIEventKind.AGENT
    assert event.level is UIEventLevel.DEBUG
    assert isinstance(event.payload, RuntimeEventPayload)
    assert isinstance(event.payload.event.payload, ToolCallStarted)
    assert event.payload.event.payload.tool_name == "shell"


def test_agent_event_bridge_exposes_tool_correlation_and_outcome() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(
        AgentEvent.tool_call_end("shell", "ok", tool_call_id="call-7", success=True)
    )

    event = seen[0]
    assert isinstance(event.payload, RuntimeEventPayload)
    runtime = event.payload.event
    assert isinstance(runtime.payload, ToolCallFinished)
    assert runtime.payload.tool_call_id == "call-7"
    assert runtime.payload.outcome.model_text == "ok"
