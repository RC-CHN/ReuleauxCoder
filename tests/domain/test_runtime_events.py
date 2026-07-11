import json

import pytest

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolArchiveReference,
    ToolDiagnostic,
    ToolDiff,
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolTruncation,
)
from reuleauxcoder.domain.runtime.events import (
    AssistantContentDelta,
    NotificationRaised,
    ReasoningDelta,
    RuntimeEventKind,
    RuntimeEvent,
    ToolCallFinished,
    ToolCallStarted,
    TurnFinished,
    TurnStarted,
    agent_event_to_runtime_event,
)
from reuleauxcoder.domain.runtime.serialization import (
    runtime_event_from_dict,
    runtime_event_to_dict,
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


def test_adapter_preserves_agent_and_session_generation() -> None:
    legacy = AgentEvent.chat_start("hello")
    legacy.agent_id = "agent-1"
    legacy.session_generation = 7

    runtime = agent_event_to_runtime_event(legacy)

    assert runtime.agent_id == "agent-1"
    assert runtime.session_generation == 7


def test_legacy_turn_and_stream_events_map_to_canonical_payloads() -> None:
    assert isinstance(
        agent_event_to_runtime_event(AgentEvent.chat_start("hello")).payload,
        TurnStarted,
    )
    assert isinstance(
        agent_event_to_runtime_event(AgentEvent.chat_end("done")).payload,
        TurnFinished,
    )
    assert isinstance(
        agent_event_to_runtime_event(AgentEvent.stream_token("a")).payload,
        AssistantContentDelta,
    )
    assert isinstance(
        agent_event_to_runtime_event(AgentEvent.stream_reasoning("r")).payload,
        ReasoningDelta,
    )


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


def test_runtime_event_json_round_trip_preserves_structured_tool_outcome() -> None:
    event = RuntimeEvent(
        payload=ToolCallFinished(
            tool_call_id="call-1",
            tool_name="edit_file",
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                summary="edit failed",
                stdout="partial",
                stderr="broken",
                diff=ToolDiff(path="main.py", unified="--- a/main.py\n+++ b/main.py\n"),
                diagnostics=(
                    ToolDiagnostic(
                        path="main.py",
                        line=2,
                        character=4,
                        message="invalid",
                        severity="error",
                    ),
                ),
                exit_code=1,
                duration_seconds=0.25,
                truncation=ToolTruncation(100, 10, 20, 2),
                archive_reference=ToolArchiveReference("/tmp/output.txt"),
                metadata={"attempt": 2, "labels": ["lsp"]},
                error_kind=ToolErrorKind.EXECUTION,
                model_content="bounded",
            ),
        ),
        event_id="event-1",
        timestamp=123.5,
        agent_id="agent-1",
        session_generation=3,
        session_id="session-1",
        turn_id="turn-1",
        correlation_id="call-1",
    )

    encoded = json.loads(json.dumps(runtime_event_to_dict(event)))

    assert runtime_event_from_dict(encoded) == event


def test_runtime_event_codec_rejects_unknown_version_and_payload() -> None:
    event = RuntimeEvent(payload=TurnStarted("hello"), event_id="event-1")
    encoded = runtime_event_to_dict(event)
    encoded["version"] = 999
    with pytest.raises(ValueError, match="Unsupported runtime event version"):
        runtime_event_from_dict(encoded)

    encoded = runtime_event_to_dict(event)
    encoded["payload"]["type"] = "OpaquePayload"
    with pytest.raises(ValueError, match="Unknown runtime payload type"):
        runtime_event_from_dict(encoded)
