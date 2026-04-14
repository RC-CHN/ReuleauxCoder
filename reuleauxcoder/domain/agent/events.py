"""Agent events - event types for telemetry and hooks."""

import time
from dataclasses import dataclass, field
from typing import Optional, Any
from enum import Enum


class AgentEventType(Enum):
    """Types of agent events."""

    CHAT_START = "chat_start"
    CHAT_END = "chat_end"
    STREAM_TOKEN = "stream_token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    SUBAGENT_COMPLETED = "subagent_completed"
    COMPRESSION_START = "compression_start"
    COMPRESSION_END = "compression_end"
    ERROR = "error"


@dataclass
class AgentEvent:
    """An event emitted by the agent during execution."""

    event_type: AgentEventType
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    # Tool call specific fields
    tool_name: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[str] = None
    tool_success: Optional[bool] = None

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
    def chat_end(cls, response: str) -> "AgentEvent":
        """Create a chat end event."""
        return cls(
            event_type=AgentEventType.CHAT_END,
            data={"response": response},
        )

    @classmethod
    def tool_call_start(cls, tool_name: str, tool_args: dict) -> "AgentEvent":
        """Create a tool call start event."""
        return cls(
            event_type=AgentEventType.TOOL_CALL_START,
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
    ) -> "AgentEvent":
        """Create a tool call end event."""
        return cls(
            event_type=AgentEventType.TOOL_CALL_END,
            tool_name=tool_name,
            tool_result=result[:500]
            if len(result) > 500
            else result,  # Truncate for events
            tool_success=success,
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
    def error(cls, message: str) -> "AgentEvent":
        """Create an error event."""
        return cls(
            event_type=AgentEventType.ERROR,
            error_message=message,
        )
