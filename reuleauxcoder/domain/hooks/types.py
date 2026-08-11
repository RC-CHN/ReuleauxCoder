"""Hook type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Callable, Mapping
from typing import Any

from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome


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
    agent_id: str | None = None
    session_generation: int | None = None
    session_id: str | None = None
    turn_id: str | None = None
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
    outcome: ToolOutcome | None = None
    round_index: int | None = None


@dataclass(slots=True)
class BeforeLLMRequestContext(HookContext):
    """Context before sending a request to the LLM."""

    request_params: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    _dispatch_callbacks: list[Callable[[BeforeLLMRequestContext], None]] = field(
        default_factory=list,
        repr=False,
    )
    _dispatch_payload_changed: bool = field(default=False, init=False, repr=False)

    def defer_until_dispatch(
        self,
        callback: Callable[[BeforeLLMRequestContext], None],
    ) -> None:
        """Commit a transform side effect at the final provider handoff."""
        self._dispatch_callbacks.append(callback)

    def _transfer_dispatch_callbacks_to(
        self,
        target: BeforeLLMRequestContext,
    ) -> None:
        """Preserve deferred state when a transform replaces its context."""
        if target is self:
            return
        if self._dispatch_callbacks:
            callbacks = tuple(self._dispatch_callbacks)
            self._dispatch_callbacks.clear()
            target._dispatch_callbacks[0:0] = callbacks
        if self._dispatch_payload_changed:
            target._dispatch_payload_changed = True
            self._dispatch_payload_changed = False

    def _commit_dispatch_callbacks(self) -> tuple[Exception, ...]:
        callbacks = tuple(self._dispatch_callbacks)
        self._dispatch_callbacks.clear()
        failures: list[Exception] = []
        for callback in callbacks:
            try:
                callback(self)
            except Exception as error:
                failures.append(error)
        return tuple(failures)

    def _has_dispatch_callbacks(self) -> bool:
        return bool(self._dispatch_callbacks)

    def mark_dispatch_payload_changed(self) -> None:
        """Request one final estimate refresh after a callback edits payload."""
        self._dispatch_payload_changed = True

    def _consume_dispatch_payload_changed(self) -> bool:
        changed = self._dispatch_payload_changed
        self._dispatch_payload_changed = False
        return changed


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


@dataclass(frozen=True, slots=True)
class HookContextSnapshot:
    """Immutable observer input detached from transform control flow."""

    hook_point: HookPoint
    agent_id: str | None
    session_generation: int | None
    session_id: str | None
    turn_id: str | None
    trace_id: str | None
    metadata: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    """Structured, observable failure raised by one hook stage."""

    hook_name: str
    hook_point: HookPoint
    hook_kind: HookKind
    message: str
    severity: str = "warning"
