"""Hook type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall


class HookKind(str, Enum):
    """Semantic categories of hooks."""

    GUARD = "guard"
    TRANSFORM = "transform"
    OBSERVER = "observer"


class HookPoint(str, Enum):
    """Supported hook points for the MVP runtime."""

    BEFORE_TOOL_EXECUTE = "before_tool_execute"
    AFTER_TOOL_EXECUTE = "after_tool_execute"
    BEFORE_LLM_REQUEST = "before_llm_request"
    AFTER_LLM_RESPONSE = "after_llm_response"
    RUNNER_STARTUP = "runner_startup"
    RUNNER_SHUTDOWN = "runner_shutdown"
    SESSION_START = "session_start"
    SESSION_SAVE = "session_save"


@dataclass(slots=True)
class HookContext:
    """Base context passed through hook execution."""

    hook_point: HookPoint
    session_id: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BeforeToolExecuteContext(HookContext):
    """Context before a tool executes."""

    tool_call: ToolCall | None = None
    round_index: int | None = None


@dataclass(slots=True)
class AfterToolExecuteContext(HookContext):
    """Context after a tool executes."""

    tool_call: ToolCall | None = None
    result: str = ""
    round_index: int | None = None


@dataclass(slots=True)
class BeforeLLMRequestContext(HookContext):
    """Context before sending a request to the LLM."""

    request_params: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None


@dataclass(slots=True)
class AfterLLMResponseContext(HookContext):
    """Context after receiving an LLM response."""

    request_params: dict[str, Any] = field(default_factory=dict)
    response: LLMResponse | None = None
    model: str | None = None


@dataclass(slots=True)
class RunnerStartupContext(HookContext):
    """Context when the application runner finishes startup."""

    config: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunnerShutdownContext(HookContext):
    """Context when the application runner begins shutdown."""

    pass


@dataclass(slots=True)
class SessionStartContext(HookContext):
    """Context when a new session starts."""

    pass


@dataclass(slots=True)
class SessionSaveContext(HookContext):
    """Context when a session is being saved."""

    session_id: str | None = None
    session_data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GuardDecision:
    """Explicit guard decision result."""

    allowed: bool
    reason: str | None = None
    warning: str | None = None
    requires_approval: bool = False

    @classmethod
    def allow(cls) -> "GuardDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "GuardDecision":
        return cls(allowed=False, reason=reason)

    @classmethod
    def warn(cls, warning: str) -> "GuardDecision":
        return cls(allowed=True, warning=warning)

    @classmethod
    def require_approval(cls, reason: str | None = None) -> "GuardDecision":
        return cls(allowed=True, reason=reason, requires_approval=True)
