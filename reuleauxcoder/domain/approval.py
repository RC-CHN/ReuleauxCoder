"""Approval provider abstractions, shared provider, and pending bridge."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

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


# ── PendingApproval: unified bridge between tool request and UI resolution ──


@dataclass
class PendingApproval:
    """Bridges an approval request to the UI that will resolve it.

    Lifecycle:
      1. SharedApprovalProvider creates this with a threading.Event.
      2. The handler fills ``decision`` and calls ``resolve()`` (event.set).
         - CLI: handler resolves in the same thread, so event is already set
           when ``wait()`` is called → returns immediately.
         - TUI: handler puts the pending onto a channel; the TUI dialog
           resolves it later (another thread) → ``wait()`` blocks until then.
      3. SharedApprovalProvider calls ``wait()`` → returns decision or
         timeout-denied.

    Timeout defaults to 60 s.  On timeout the provider returns deny_once —
    the only safe default for unapproved tool calls.
    """

    request: ApprovalRequest
    event: threading.Event = field(default_factory=threading.Event)
    decision: ApprovalDecision | None = None
    timeout: float = 60.0

    def wait(self) -> bool:
        """Block until resolved or timeout. Returns ``True`` if resolved."""
        return self.event.wait(timeout=self.timeout)

    def resolve(self, decision: ApprovalDecision) -> None:
        """Called by the handler to set the decision and signal the event."""
        self.decision = decision
        self.event.set()


# ── ApprovalProvider (Protocol) ─────────────────────────────────────────


class ApprovalProvider(Protocol):
    """Interface-specific approval interaction."""

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """Block until the user approves or denies execution."""


ApprovalHandler = Callable[[PendingApproval], None]
"""A handler that resolves a pending approval.

CLI:  resolves synchronously (same thread) — ``resolve()`` is called
      before ``SharedApprovalProvider`` reaches ``wait()``.
TUI:  pushes the pending onto ``AgentChannels.approvals``; a Textual
      ``ModalScreen`` calls ``resolve()`` later.
"""


class SharedApprovalProvider(ApprovalProvider):
    """Unified approval provider — handler determines CLI / TUI behaviour."""

    def __init__(self, handler: ApprovalHandler):
        self._handler = handler

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        pending = PendingApproval(request=request)
        self._handler(pending)

        if pending.wait():
            return pending.decision or ApprovalDecision.deny_once("no decision")
        return ApprovalDecision.deny_once(
            f"approval timed out after {pending.timeout}s"
        )
