"""Agent events - event types for telemetry and hooks."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome


class AgentEventType(Enum):
    """Types of agent events."""

    CHAT_START = "chat_start"
    CHAT_END = "chat_end"
    STREAM_TOKEN = "stream_token"
    STREAM_REASONING = "stream_reasoning"
    TOOL_CALL_START = "tool_call_start"
    TOOL_OUTPUT_DELTA = "tool_output_delta"
    TOOL_CALL_END = "tool_call_end"
    SUBAGENT_COMPLETED = "subagent_completed"
    COMPRESSION_START = "compression_start"
    COMPRESSION_END = "compression_end"
    ERROR = "error"
    DIAGNOSTIC = "diagnostic"


@dataclass
class AgentEvent:
    """An event emitted by the agent during execution."""

    event_type: AgentEventType
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    agent_id: Optional[str] = None
    session_generation: Optional[int] = None
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    correlation_id: Optional[str] = None
    data: dict = field(default_factory=dict)

    # Tool call specific fields
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[str] = None
    tool_success: Optional[bool] = None
    tool_outcome: Optional[ToolOutcome] = None

    # Error specific fields
    error_message: Optional[str] = None

    @classmethod
    def chat_start(cls, user_input: str) -> "AgentEvent":
        """Create a chat start event."""
        return cls(
            event_type=AgentEventType.CHAT_START,
            data={"user_input": user_input},
        )

    @classmethod
    def chat_end(cls, response: str, *, render_response: bool = True) -> "AgentEvent":
        """Create a chat end event."""
        return cls(
            event_type=AgentEventType.CHAT_END,
            data={"response": response, "render_response": render_response},
        )

    @classmethod
    def tool_call_start(
        cls,
        tool_name: str,
        tool_args: dict,
        *,
        tool_call_id: str | None = None,
    ) -> "AgentEvent":
        """Create a tool call start event."""
        return cls(
            event_type=AgentEventType.TOOL_CALL_START,
            correlation_id=tool_call_id,
            tool_name=tool_name,
            tool_args=tool_args,
        )

    @classmethod
    def tool_call_end(
        cls,
        tool_name: str,
        result: str,
        *,
        success: bool = True,
        tool_call_id: str | None = None,
        outcome: ToolOutcome | None = None,
    ) -> "AgentEvent":
        """Create a tool call end event."""
        effective_outcome = outcome or ToolOutcome.from_legacy(
            result, success=success
        )
        return cls(
            event_type=AgentEventType.TOOL_CALL_END,
            correlation_id=tool_call_id,
            tool_name=tool_name,
            tool_result=result,
            tool_success=effective_outcome.success,
            tool_outcome=effective_outcome,
        )

    @classmethod
    def tool_output_delta(
        cls,
        tool_name: str,
        text: str,
        *,
        stream: str = "stdout",
        tool_call_id: str | None = None,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.TOOL_OUTPUT_DELTA,
            correlation_id=tool_call_id,
            tool_name=tool_name,
            data={"text": text, "stream": stream},
        )

    @classmethod
    def subagent_completed(
        cls,
        *,
        job_id: str,
        mode: str,
        task: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> "AgentEvent":
        """Create a sub-agent completion event."""
        return cls(
            event_type=AgentEventType.SUBAGENT_COMPLETED,
            data={
                "job_id": job_id,
                "mode": mode,
                "task": task,
                "status": status,
                "result": result,
                "error": error,
            },
        )

    @classmethod
    def stream_token(cls, token: str) -> "AgentEvent":
        """Create a stream token event."""
        return cls(
            event_type=AgentEventType.STREAM_TOKEN,
            data={"token": token},
        )

    @classmethod
    def stream_reasoning(cls, token: str) -> "AgentEvent":
        """Create a stream reasoning event."""
        return cls(
            event_type=AgentEventType.STREAM_REASONING,
            data={"token": token},
        )

    @classmethod
    def error(cls, message: str) -> "AgentEvent":
        """Create an error event."""
        return cls(
            event_type=AgentEventType.ERROR,
            error_message=message,
        )

    @classmethod
    def diagnostic(
        cls,
        message: str,
        *,
        code: str,
        severity: str = "warning",
        details: dict | None = None,
    ) -> "AgentEvent":
        """Create a structured non-fatal runtime diagnostic."""
        return cls(
            event_type=AgentEventType.DIAGNOSTIC,
            data={
                "message": message,
                "code": code,
                "severity": severity,
                "details": dict(details or {}),
            },
        )
