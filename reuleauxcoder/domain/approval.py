"""Approval provider abstractions and CLI implementation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ApprovalDecisionMode = Literal["allow_once", "deny_once"]


@dataclass(slots=True)
class ApprovalRequest:
    """A request asking the interface layer whether a tool may proceed."""

    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_source: str = "unknown"
    effect_class: str | None = None
    reason: str | None = None
    profile: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalDecision:
    """A user-facing approval decision."""

    mode: ApprovalDecisionMode
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.mode == "allow_once"

    @classmethod
    def allow_once(cls, reason: str | None = None) -> "ApprovalDecision":
        return cls(mode="allow_once", reason=reason)

    @classmethod
    def deny_once(cls, reason: str | None = None) -> "ApprovalDecision":
        return cls(mode="deny_once", reason=reason)


class ApprovalProvider(Protocol):
    """Interface-specific approval interaction."""

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Block until the user approves or denies execution."""
