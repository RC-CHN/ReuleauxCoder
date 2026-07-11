import pytest

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.runtime.events import (
    NotificationRaised,
    RuntimeEventKind,
    ToolCallFinished,
    ToolCallStarted,
    agent_event_to_runtime_event,
)


def test_tool_start_adapter_preserves_correlation_and_context() -> None:
    legacy = AgentEvent.tool_call_start(
        "shell", {"command": "pwd"}, tool_call_id="call-42"
    )
    runtime = agent_event_to_runtime_event(
        legacy, session_id="session-1", turn_id="turn-2"
    )

    assert runtime.kind is RuntimeEventKind.TOOL_CALL_STARTED
    assert runtime.correlation_id == "call-42"
    assert runtime.session_id == "session-1"
    assert runtime.turn_id == "turn-2"
    assert isinstance(runtime.payload, ToolCallStarted)
    assert runtime.payload.tool_call_id == "call-42"
    assert runtime.payload.arguments == {"command": "pwd"}


def test_tool_end_adapter_preserves_full_structured_outcome() -> None:
    result = "x" * 10_000
    legacy = AgentEvent.tool_call_end(
        "read_file", result, success=False, tool_call_id="call-42"
    )
    runtime = agent_event_to_runtime_event(legacy)

    assert isinstance(runtime.payload, ToolCallFinished)
    assert runtime.payload.tool_call_id == "call-42"
    assert runtime.payload.outcome.model_text == result
    assert runtime.payload.outcome.success is False


def test_legacy_tool_event_without_call_id_gets_stable_event_fallback() -> None:
    legacy = AgentEvent.tool_call_start("shell", {})

    runtime = agent_event_to_runtime_event(legacy)

    assert runtime.correlation_id is None
    assert isinstance(runtime.payload, ToolCallStarted)
    assert runtime.payload.tool_call_id == legacy.event_id


def test_unsupported_legacy_event_fails_loudly() -> None:
    legacy = AgentEvent(event_type=AgentEventType.COMPRESSION_START)

    with pytest.raises(ValueError, match="Unsupported legacy agent event"):
        agent_event_to_runtime_event(legacy)


def test_structured_diagnostic_becomes_runtime_notification() -> None:
    event = AgentEvent.diagnostic(
        "observer failed",
        code="hook.failure",
        details={"hook_name": "demo"},
    )

    runtime = agent_event_to_runtime_event(event)

    assert runtime.kind is RuntimeEventKind.NOTIFICATION_RAISED
    assert isinstance(runtime.payload, NotificationRaised)
    assert runtime.payload.code == "hook.failure"
    assert runtime.payload.details == {"hook_name": "demo"}
