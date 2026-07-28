"""Approval provider abstractions, shared provider, and pending bridge."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Protocol

from reuleauxcoder.domain.approval_engine import approval_pattern_matches
from reuleauxcoder.domain.config.models import ApprovalRuleConfig

ApprovalDecisionMode = Literal["allow_once", "allow_session", "deny_once"]


class ApprovalSectionKind(str, Enum):
    TEXT = "text"
    DIFF = "diff"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class ApprovalSection:
    id: str
    title: str
    kind: ApprovalSectionKind
    content: str | Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ApprovalPreview:
    """Adapter-neutral review content built once before user interaction."""

    sections: tuple[ApprovalSection, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalGrantScope:
    """Tool-owned candidate scope before policy dimensions are attached."""

    id: str
    label: str
    description: str
    patterns: tuple[str, ...] = ()
    broad: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalGrantCandidate:
    """Validated session grant offered as one atomic user choice."""

    id: str
    label: str
    description: str
    proposed_rules: tuple[ApprovalRuleConfig, ...]
    scope_key: str | None = None
    broad: bool = False


@dataclass(slots=True)
class ApprovalQueueStatus:
    """Live queue facts shared with the currently focused review UI."""

    position: int = 1
    waiting: int = 0


@dataclass(slots=True)
class ApprovalRequest:
    """A request asking the interface layer whether a tool may proceed."""

    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_source: str = "unknown"
    mcp_server: str | None = None
    effect_class: str | None = None
    reason: str | None = None
    profile: str | None = None
    subjects: tuple[str, ...] = ()
    scope_key: str | None = None
    grant_candidates: tuple[ApprovalGrantCandidate, ...] = ()
    queue_status: ApprovalQueueStatus = field(default_factory=ApprovalQueueStatus)
    metadata: dict[str, Any] = field(default_factory=dict)
    preview: ApprovalPreview | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(slots=True)
class ApprovalDecision:
    """A user-facing approval decision."""

    mode: ApprovalDecisionMode
    reason: str | None = None
    reviewed: bool = False
    grant: ApprovalGrantCandidate | None = None
    released_request_ids: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.mode in {"allow_once", "allow_session"}

    @classmethod
    def allow_once(
        cls, reason: str | None = None, *, reviewed: bool = False
    ) -> "ApprovalDecision":
        return cls(mode="allow_once", reason=reason, reviewed=reviewed)

    @classmethod
    def deny_once(
        cls, reason: str | None = None, *, reviewed: bool = False
    ) -> "ApprovalDecision":
        return cls(mode="deny_once", reason=reason, reviewed=reviewed)

    @classmethod
    def allow_session(
        cls,
        grant: ApprovalGrantCandidate,
        reason: str | None = None,
        *,
        reviewed: bool = False,
    ) -> "ApprovalDecision":
        return cls(
            mode="allow_session",
            reason=reason,
            reviewed=reviewed,
            grant=grant,
        )


def approval_grant_covers_request(
    grant: ApprovalGrantCandidate,
    request: ApprovalRequest,
) -> bool:
    """Return whether every resource in a request is covered by one grant."""
    if grant.scope_key != request.scope_key:
        return False
    rules = tuple(rule for rule in grant.proposed_rules if rule.action == "allow")
    if not rules:
        return False

    def dimensions_match(rule: ApprovalRuleConfig) -> bool:
        return (
            (rule.tool_name is None or rule.tool_name == request.tool_name)
            and (rule.tool_source is None or rule.tool_source == request.tool_source)
            and (rule.mcp_server is None or rule.mcp_server == request.mcp_server)
            and (
                rule.effect_class is None
                or rule.effect_class == request.effect_class
            )
            and (rule.profile is None or rule.profile == request.profile)
            and (rule.scope_key is None or rule.scope_key == request.scope_key)
        )

    matching = tuple(rule for rule in rules if dimensions_match(rule))
    if not matching:
        return False
    if not request.subjects:
        return any(rule.pattern is None for rule in matching)
    return all(
        any(approval_pattern_matches(rule.pattern, subject) for rule in matching)
        for subject in request.subjects
    )


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
        ...


ApprovalHandler = Callable[[PendingApproval], None]
"""A handler that resolves a pending approval.

CLI:  resolves synchronously (same thread) — ``resolve()`` is called
      before ``SharedApprovalProvider`` reaches ``wait()``.
TUI:  pushes the pending onto a queue; a Textual ``ModalScreen``
      calls ``resolve()`` later.
"""

ApprovalJudge = Callable[["ApprovalRequest"], "ApprovalDecision | None"]
"""An optional policy judge that can short-circuit the human handler.

Returns ``ApprovalDecision`` to auto-approve/deny without user input,
or ``None`` to escalate to the human handler. Judges are policy mechanisms;
sub-agents do not use a parent model as an authorization source.
"""

ApprovalRequestObserver = Callable[["ApprovalRequest"], None]
ApprovalDecisionObserver = Callable[["ApprovalRequest", "ApprovalDecision"], None]
SessionGrantHandler = Callable[
    ["ApprovalRequest", "ApprovalGrantCandidate"],
    None,
]


class SharedApprovalProvider(ApprovalProvider):
    """Unified approval provider — handler determines CLI / TUI behaviour.

    Optional *judges* run before the human handler.  Each judge may
    return an ``ApprovalDecision`` (auto-resolve) or ``None``
    (escalate).
    """

    def __init__(
        self,
        handler: ApprovalHandler,
        *,
        judges: list[ApprovalJudge] | None = None,
        coordinator: "ApprovalCoordinator | None" = None,
        reviewer: Literal["user", "auto_review"] = "user",
        on_request: ApprovalRequestObserver | None = None,
        on_decision: ApprovalDecisionObserver | None = None,
        on_session_grant: SessionGrantHandler | None = None,
    ):
        self._coordinator = coordinator or ApprovalCoordinator(
            handler,
            on_session_grant=on_session_grant,
        )
        self._judges: list[ApprovalJudge] = judges or []
        self._reviewer = reviewer
        self._on_request = on_request
        self._on_decision = on_decision

    @property
    def handler(self) -> ApprovalHandler:
        """The human handler (CLI interactor or TUI queue pusher)."""
        return self._coordinator.handler

    @property
    def coordinator(self) -> "ApprovalCoordinator":
        """Root-scoped coordinator shared by parent and child requests."""
        return self._coordinator

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        force_human_review = bool(request.metadata.get("force_human_review"))
        request.metadata["reviewer"] = "user" if force_human_review else self._reviewer
        if self._on_request is not None:
            self._on_request(request)
        try:
            if force_human_review:
                decision = self._coordinator.request_approval(request)
            else:
                for judge in self._judges:
                    decision = judge(request)
                    if decision is not None:
                        break
                else:
                    decision = self._coordinator.request_approval(request)
        except BaseException:
            interrupted = ApprovalDecision.deny_once("approval interrupted")
            if self._on_decision is not None:
                self._on_decision(request, interrupted)
            raise
        if self._on_decision is not None:
            self._on_decision(request, decision)
        return decision


class ApprovalCoordinator(ApprovalProvider):
    """FIFO root interaction coordinator with one human-review focus.

    Calls may register concurrently. Only the queue head is presented to the
    interface, so background agents cannot stack terminal prompts or dialogs.
    The lock protects queue state only and is never held while the user is
    reviewing a request.
    """

    def __init__(
        self,
        handler: ApprovalHandler,
        *,
        timeout: float = 60.0,
        on_session_grant: SessionGrantHandler | None = None,
    ):
        self._handler = handler
        self._timeout = max(0.01, float(timeout))
        self._on_session_grant = on_session_grant
        self._condition = threading.Condition()
        self._queue: deque[PendingApproval] = deque()

    @property
    def handler(self) -> ApprovalHandler:
        return self._handler

    @property
    def pending_count(self) -> int:
        """Number of registered requests, including the focused request."""
        with self._condition:
            return len(self._queue)

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        pending = PendingApproval(request=request, timeout=self._timeout)
        with self._condition:
            self._queue.append(pending)
            self._refresh_queue_status_locked()
            self._condition.notify_all()
            while (
                self._queue
                and self._queue[0] is not pending
                and not pending.event.is_set()
            ):
                self._condition.wait()
            if pending.event.is_set():
                return pending.decision or ApprovalDecision.deny_once(
                    "approval cancelled"
                )

        try:
            try:
                self._handler(pending)
            except (KeyboardInterrupt, EOFError):
                raise
            except Exception as error:
                return ApprovalDecision.deny_once(
                    f"approval interaction failed closed: {error}"
                )

            if pending.wait():
                decision = pending.decision or ApprovalDecision.deny_once("no decision")
            else:
                decision = ApprovalDecision.deny_once(
                    f"approval timed out after {pending.timeout}s"
                )
            if decision.mode == "allow_session":
                decision = self._apply_session_grant(request, decision)
            return decision
        finally:
            with self._condition:
                if self._queue and self._queue[0] is pending:
                    self._queue.popleft()
                else:
                    try:
                        self._queue.remove(pending)
                    except ValueError:
                        pass
                self._refresh_queue_status_locked()
                self._condition.notify_all()

    def _apply_session_grant(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecision,
    ) -> ApprovalDecision:
        grant = decision.grant
        if grant is None:
            return ApprovalDecision.deny_once(
                "session approval did not include a validated grant"
            )
        if self._on_session_grant is None:
            return ApprovalDecision.deny_once(
                "session approval is unavailable in this runtime"
            )
        try:
            self._on_session_grant(request, grant)
        except Exception as error:
            return ApprovalDecision.deny_once(
                f"session approval failed closed: {error}"
            )

        covered_ids: list[str] = []
        with self._condition:
            for pending in tuple(self._queue):
                if pending.request.request_id == request.request_id:
                    continue
                if pending.event.is_set():
                    continue
                if not approval_grant_covers_request(grant, pending.request):
                    continue
                try:
                    self._queue.remove(pending)
                except ValueError:  # pragma: no cover - protected by condition
                    continue
                pending.resolve(
                    ApprovalDecision.allow_session(
                        grant,
                        (
                            "approved by a matching session grant from "
                            f"request {request.request_id}"
                        ),
                        reviewed=False,
                    )
                )
                covered_ids.append(pending.request.request_id)
            if covered_ids:
                self._refresh_queue_status_locked()
                self._condition.notify_all()
        decision.released_request_ids = tuple(covered_ids)
        return decision

    def cancel_matching(
        self,
        predicate: Callable[[ApprovalRequest], bool],
        *,
        reason: str,
    ) -> tuple[str, ...]:
        """Fail closed queued/focused approvals matching one runtime owner."""
        with self._condition:
            matches = [pending for pending in self._queue if predicate(pending.request)]
            for pending in matches:
                try:
                    self._queue.remove(pending)
                except ValueError:  # pragma: no cover - lock makes this defensive
                    continue
                pending.resolve(ApprovalDecision.deny_once(reason))
            if matches:
                self._refresh_queue_status_locked()
                self._condition.notify_all()
            return tuple(pending.request.request_id for pending in matches)

    def _refresh_queue_status_locked(self) -> None:
        total = len(self._queue)
        for index, pending in enumerate(self._queue):
            pending.request.queue_status.position = index + 1
            pending.request.queue_status.waiting = max(0, total - index - 1)
