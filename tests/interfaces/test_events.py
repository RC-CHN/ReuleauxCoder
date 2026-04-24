from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.interfaces.events import (
    AgentEventBridge,
    UIEvent,
    UIEventBus,
    UIEventKind,
    UIEventLevel,
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


def test_ui_event_bus_open_view_emits_structured_view_event() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    bus.open_view(
        "skills",
        title="Skills",
        payload={"markdown": "# Skills"},
        focus=False,
        reuse_key="skills",
    )

    event = seen[0]
    assert event.kind is UIEventKind.VIEW
    assert event.data["action"] == "open"
    assert event.data["view_type"] == "skills"
    assert event.data["title"] == "Skills"
    assert event.data["payload"] == {"markdown": "# Skills"}
    assert event.data["focus"] is False
    assert event.data["reuse_key"] == "skills"


def test_agent_event_bridge_maps_error_to_error_level() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(AgentEvent.error("boom"))

    event = seen[0]
    assert event.kind is UIEventKind.AGENT
    assert event.level is UIEventLevel.ERROR
    assert event.data["error_message"] == "boom"


def test_agent_event_bridge_maps_tool_events_to_debug_level() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(
        AgentEvent.tool_call_start("shell", {"command": "ls"})
    )

    event = seen[0]
    assert event.kind is UIEventKind.AGENT
    assert event.level is UIEventLevel.DEBUG
    assert event.data["tool_name"] == "shell"
