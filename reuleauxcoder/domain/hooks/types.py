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
class GuardDecision:
    """Explicit guard decision result."""

    allowed: bool
    reason: str | None = None
    warning: str | None = None

    @classmethod
    def allow(cls) -> "GuardDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "GuardDecision":
        return cls(allowed=False, reason=reason)

    @classmethod
    def warn(cls, warning: str) -> "GuardDecision":
        return cls(allowed=True, warning=warning)
