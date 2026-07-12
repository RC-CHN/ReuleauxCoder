"""Strongly typed runtime events.

This module deliberately has no UI or transport dependencies.  CLI, TUI and
remote protocol adapters consume the same envelope and typed payloads.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome


class RuntimeEventKind(str, Enum):
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"
    CHAT_STARTED = "turn_started"
    CHAT_COMPLETED = "turn_finished"
    ASSISTANT_CONTENT_DELTA = "assistant_content_delta"
    REASONING_DELTA = "reasoning_delta"
    STREAM_CHUNK = "stream_chunk"
    REASONING_CHUNK = "reasoning_chunk"
    TOOL_STARTED = "tool_started"
    TOOL_OUTPUT_DELTA = "tool_output_delta"
    TOOL_FINISHED = "tool_finished"
    TOOL_CALL_STARTED = "tool_started"
    TOOL_CALL_FINISHED = "tool_finished"
    SUBAGENT_JOB_CHANGED = "subagent_job_changed"
    SUBAGENT_FINISHED = "subagent_job_changed"
    DIAGNOSTICS_PUBLISHED = "diagnostics_published"
    DIAGNOSTICS_CLEARED = "diagnostics_cleared"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    ERROR_OCCURRED = "error_occurred"
    NOTIFICATION_RAISED = "notification_raised"
    SESSION_CHANGED = "session_changed"
    RUNTIME_STATE_CHANGED = "runtime_state_changed"
    VIEW_REQUESTED = "view_requested"
    VIEW_REFRESHED = "view_refreshed"
    PLAN_UPDATED = "plan_updated"
    PROGRESS_REPORTED = "progress_reported"


@dataclass(frozen=True)
class TurnStarted:
    user_input: str
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.TURN_STARTED, init=False
    )


@dataclass(frozen=True)
class TurnFinished:
    response: str
    render_response: bool = True
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.TURN_FINISHED, init=False
    )


@dataclass(frozen=True)
class AssistantContentDelta:
    text: str
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.ASSISTANT_CONTENT_DELTA, init=False
    )


@dataclass(frozen=True)
class ReasoningDelta:
    text: str
    display_mode: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.REASONING_DELTA, init=False
    )


@dataclass(frozen=True)
class ChatStarted:
    user_input: str
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.CHAT_STARTED, init=False
    )


@dataclass(frozen=True)
class ChatCompleted:
    response: str
    render_response: bool = True
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.CHAT_COMPLETED, init=False
    )


@dataclass(frozen=True)
class StreamChunk:
    text: str
    reasoning: bool = False
    display_mode: str | None = None

    @property
    def kind(self) -> RuntimeEventKind:
        return (
            RuntimeEventKind.REASONING_CHUNK
            if self.reasoning
            else RuntimeEventKind.STREAM_CHUNK
        )


@dataclass(frozen=True)
class ToolCallStarted:
    tool_call_id: str
    tool_name: str
    arguments: dict
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.TOOL_CALL_STARTED, init=False
    )


@dataclass(frozen=True)
class ToolCallFinished:
    tool_call_id: str
    tool_name: str
    outcome: ToolOutcome
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.TOOL_CALL_FINISHED, init=False
    )


@dataclass(frozen=True)
class ToolOutputDelta:
    tool_call_id: str
    text: str
    stream: str = "stdout"
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.TOOL_OUTPUT_DELTA, init=False
    )


# Temporary import compatibility while ToolCall-prefixed consumers migrate.
ToolStarted = ToolCallStarted
ToolFinished = ToolCallFinished


@dataclass(frozen=True)
class RuntimeDiagnostic:
    line: int
    character: int
    message: str
    severity: str = "error"
    code: str | None = None


@dataclass(frozen=True)
class DiagnosticsPublished:
    batch_id: str
    file_path: str
    document_version: int
    diagnostic_generation: int
    diagnostics: tuple[RuntimeDiagnostic, ...]
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.DIAGNOSTICS_PUBLISHED, init=False
    )


@dataclass(frozen=True)
class DiagnosticsCleared:
    batch_id: str
    file_path: str
    document_version: int
    diagnostic_generation: int
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.DIAGNOSTICS_CLEARED, init=False
    )


@dataclass(frozen=True)
class SubagentJobChanged:
    job_id: str
    mode: str
    task: str
    status: str
    result: str | None = None
    error: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.SUBAGENT_JOB_CHANGED, init=False
    )


# Temporary import compatibility while interface adapters migrate.
SubagentFinished = SubagentJobChanged


@dataclass(frozen=True)
class ApprovalRequested:
    request_id: str
    title: str
    preview: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.APPROVAL_REQUESTED, init=False
    )


@dataclass(frozen=True)
class ApprovalResolved:
    request_id: str
    approved: bool
    reason: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.APPROVAL_RESOLVED, init=False
    )


@dataclass(frozen=True)
class ErrorOccurred:
    message: str
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.ERROR_OCCURRED, init=False
    )


@dataclass(frozen=True)
class NotificationRaised:
    message: str
    code: str
    severity: str = "info"
    details: dict = field(default_factory=dict)
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.NOTIFICATION_RAISED, init=False
    )


@dataclass(frozen=True)
class SessionChanged:
    action: str
    session_id: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.SESSION_CHANGED, init=False
    )


@dataclass(frozen=True)
class RuntimeStateChanged:
    state: str
    reason: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.RUNTIME_STATE_CHANGED, init=False
    )


@dataclass(frozen=True)
class PlanUpdated:
    revision: int
    items: tuple[dict, ...]
    explanation: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.PLAN_UPDATED, init=False
    )


@dataclass(frozen=True)
class ProgressReported:
    revision: int
    phase: str
    summary: str
    next: str | None = None
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.PROGRESS_REPORTED, init=False
    )


@dataclass(frozen=True)
class ViewRequested:
    request_id: str
    view_type: str
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.VIEW_REQUESTED, init=False
    )


@dataclass(frozen=True)
class ViewRefreshed:
    request_id: str
    view_type: str
    revision: int
    kind: RuntimeEventKind = field(
        default=RuntimeEventKind.VIEW_REFRESHED, init=False
    )


RuntimePayload: TypeAlias = (
    TurnStarted
    | TurnFinished
    | AssistantContentDelta
    | ReasoningDelta
    | ChatStarted
    | ChatCompleted
    | StreamChunk
    | ToolCallStarted
    | ToolOutputDelta
    | ToolCallFinished
    | SubagentJobChanged
    | DiagnosticsPublished
    | DiagnosticsCleared
    | ApprovalRequested
    | ApprovalResolved
    | ErrorOccurred
    | NotificationRaised
    | SessionChanged
    | RuntimeStateChanged
    | PlanUpdated
    | ProgressReported
    | ViewRequested
    | ViewRefreshed
)


@dataclass(frozen=True)
class RuntimeEvent:
    """Stable envelope used for correlation, replay and transport."""

    payload: RuntimePayload
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    agent_id: str | None = None
    session_generation: int | None = None
    session_id: str | None = None
    turn_id: str | None = None
    correlation_id: str | None = None

    @property
    def kind(self) -> RuntimeEventKind:
        return self.payload.kind


def agent_event_to_runtime_event(
    event: AgentEvent,
    *,
    agent_id: str | None = None,
    session_generation: int | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> RuntimeEvent:
    """Convert a legacy ``AgentEvent`` at the migration boundary.

    Unsupported legacy event kinds fail loudly rather than becoming an opaque
    dictionary that renderers would need to guess at.
    """

    if event.event_type is AgentEventType.CHAT_START:
        payload: RuntimePayload = TurnStarted(event.data.get("user_input", ""))
    elif event.event_type is AgentEventType.CHAT_END:
        payload = TurnFinished(
            event.data.get("response", ""),
            render_response=event.data.get("render_response", True),
        )
    elif event.event_type is AgentEventType.STREAM_TOKEN:
        payload = AssistantContentDelta(event.data.get("token", ""))
    elif event.event_type is AgentEventType.STREAM_REASONING:
        payload = ReasoningDelta(
            event.data.get("token", ""),
            display_mode=event.data.get("display_mode"),
        )
    elif event.event_type is AgentEventType.TOOL_CALL_START:
        payload = ToolCallStarted(
            tool_call_id=_required_correlation_id(event),
            tool_name=event.tool_name or "unknown_tool",
            arguments=dict(event.tool_args or {}),
        )
    elif event.event_type is AgentEventType.TOOL_OUTPUT_DELTA:
        payload = ToolOutputDelta(
            tool_call_id=_required_correlation_id(event),
            text=str(event.data.get("text", "")),
            stream=str(event.data.get("stream", "stdout")),
        )
    elif event.event_type is AgentEventType.TOOL_CALL_END:
        outcome = event.tool_outcome or ToolOutcome.from_legacy(
            event.tool_result or "", success=event.tool_success is not False
        )
        payload = ToolCallFinished(
            tool_call_id=_required_correlation_id(event),
            tool_name=event.tool_name or "unknown_tool",
            outcome=outcome,
        )
    elif event.event_type is AgentEventType.SUBAGENT_COMPLETED:
        payload = SubagentJobChanged(
            job_id=str(event.data.get("job_id", "")),
            mode=str(event.data.get("mode", "")),
            task=str(event.data.get("task", "")),
            status=str(event.data.get("status", "")),
            result=event.data.get("result"),
            error=event.data.get("error"),
        )
    elif event.event_type is AgentEventType.ERROR:
        payload = ErrorOccurred(event.error_message or "Unknown agent error")
    elif event.event_type is AgentEventType.DIAGNOSTIC:
        payload = NotificationRaised(
            message=str(event.data.get("message", "Runtime diagnostic")),
            code=str(event.data.get("code", "runtime.diagnostic")),
            severity=str(event.data.get("severity", "warning")),
            details=dict(event.data.get("details") or {}),
        )
    elif event.event_type is AgentEventType.PLAN_UPDATED:
        payload = PlanUpdated(
            revision=int(event.data.get("revision", 0)),
            items=tuple(dict(item) for item in event.data.get("items", ())),
            explanation=event.data.get("explanation"),
        )
    elif event.event_type is AgentEventType.PROGRESS_REPORTED:
        payload = ProgressReported(
            revision=int(event.data.get("revision", 0)),
            phase=str(event.data.get("phase", "investigating")),
            summary=str(event.data.get("summary", "")),
            next=event.data.get("next"),
        )
    else:
        raise ValueError(f"Unsupported legacy agent event: {event.event_type.value}")

    return RuntimeEvent(
        payload=payload,
        event_id=event.event_id,
        timestamp=event.timestamp,
        agent_id=agent_id or event.agent_id,
        session_generation=(
            session_generation
            if session_generation is not None
            else event.session_generation
        ),
        session_id=session_id or event.session_id,
        turn_id=turn_id or event.turn_id,
        correlation_id=event.correlation_id,
    )


def _required_correlation_id(event: AgentEvent) -> str:
    if event.correlation_id:
        return event.correlation_id
    # Older callers did not supply an id.  The event id is stable and keeps the
    # compatibility adapter usable, but new execution paths must pass ToolCall.id.
    return event.event_id
