"""Typed terminal outcomes for asynchronous LSP diagnostics requests.

Diagnostics are fire-and-forget from the worker's point of view, but they are
not allowed to disappear from the agent's point of view.  Every accepted
request therefore ends in exactly one of these outcomes.  Only the two
``PUBLISHED_*`` states carry document diagnostics and are allowed to replace a
previous published document state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from reuleauxcoder.extensions.lsp.client import (
    LspFailureFacts,
    render_lsp_failure,
)
from reuleauxcoder.extensions.lsp.diagnostics import (
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
)


class DiagnosticOutcomeStatus(str, Enum):
    """Exactly-one terminal state for one diagnostics request."""

    PUBLISHED_NONEMPTY = "published_nonempty"
    PUBLISHED_CLEAN = "published_clean"
    TIMED_OUT = "timed_out"
    SERVER_UNAVAILABLE = "server_unavailable"
    STALE_DISCARDED = "stale_discarded"
    CANCELLED = "cancelled"
    ERROR = "error"


_PUBLISHED_STATUSES = frozenset(
    {
        DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY,
        DiagnosticOutcomeStatus.PUBLISHED_CLEAN,
    }
)
_FAILURE_STATUSES = frozenset(
    {
        DiagnosticOutcomeStatus.TIMED_OUT,
        DiagnosticOutcomeStatus.SERVER_UNAVAILABLE,
        DiagnosticOutcomeStatus.CANCELLED,
        DiagnosticOutcomeStatus.ERROR,
    }
)


def safe_observer_error_type(error: BaseException) -> str:
    """Return a bounded content-free exception type for observer diagnostics."""
    name = type(error).__name__
    safe = "".join(
        character
        for character in name
        if character.isascii() and (character.isalnum() or character in {"_", "-"})
    )[:128]
    return safe or "Error"


@dataclass(frozen=True, slots=True)
class DiagnosticOutcome:
    """Immutable result retained until an agent consumer acknowledges it."""

    batch_id: str
    route: DiagnosticRoute
    request_sequence: int
    status: DiagnosticOutcomeStatus
    created_at: float
    document_version: int | None = None
    diagnostic_generation: int | None = None
    block: DiagnosticBlock | None = None
    failure: LspFailureFacts | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, DiagnosticOutcomeStatus):
            raise TypeError("diagnostic outcome status must be typed")
        if not self.batch_id:
            raise ValueError("diagnostic outcome batch_id must be non-empty")
        if self.request_sequence < 0:
            raise ValueError("diagnostic outcome request_sequence cannot be negative")

        if self.status in _PUBLISHED_STATUSES:
            if self.block is None:
                raise ValueError("published diagnostic outcome requires a block")
            if self.document_version is None or self.diagnostic_generation is None:
                raise ValueError(
                    "published diagnostic outcome requires document generations"
                )
            is_empty = self.block.is_empty()
            if self.status is DiagnosticOutcomeStatus.PUBLISHED_CLEAN and not is_empty:
                raise ValueError("published-clean outcome must have an empty block")
            if self.status is DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY and is_empty:
                raise ValueError("published-nonempty outcome requires diagnostics")
            if self.failure is not None:
                raise ValueError("published diagnostic outcome cannot carry a failure")
            return

        if self.block is not None:
            raise ValueError("non-published diagnostic outcome cannot carry a block")
        if self.document_version is not None or self.diagnostic_generation is not None:
            raise ValueError(
                "non-published diagnostic outcome cannot claim document generations"
            )
        if self.status in _FAILURE_STATUSES and self.failure is None:
            raise ValueError("failed diagnostic outcome requires frozen failure facts")

    @property
    def is_published(self) -> bool:
        return self.status in _PUBLISHED_STATUSES

    @classmethod
    def from_batch(cls, batch: DiagnosticBatch) -> DiagnosticOutcome:
        """Project a legacy published batch into the typed terminal API."""
        return cls(
            batch_id=batch.batch_id,
            route=batch.route,
            request_sequence=batch.request_sequence,
            status=(
                DiagnosticOutcomeStatus.PUBLISHED_CLEAN
                if batch.block.is_empty()
                else DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY
            ),
            created_at=batch.created_at,
            document_version=batch.document_version,
            diagnostic_generation=batch.diagnostic_generation,
            block=batch.block,
        )


def render_diagnostic_outcome(outcome: DiagnosticOutcome) -> str | None:
    """Render a safe agent-facing terminal fact; published states need no text."""
    if outcome.is_published:
        return None

    status = outcome.status.value
    if outcome.failure is None:
        return f"LSP diagnostics ended (status={status})"
    rendered_failure = render_lsp_failure(
        outcome.failure,
        fallback_phase="diagnostics",
        fallback_error_type="Error",
    )
    return f"LSP diagnostics ended (status={status}); {rendered_failure}"


def render_diagnostic_outcomes(outcomes: tuple[DiagnosticOutcome, ...]) -> str | None:
    """Render non-published outcomes without allowing projection to crash a run."""
    rendered: list[str] = []
    for outcome in outcomes:
        try:
            item = render_diagnostic_outcome(outcome)
        except Exception as error:
            item = (
                "LSP diagnostics ended "
                "(status=error, failure_projection_error_type="
                f"{safe_observer_error_type(error)})"
            )
        if item is not None:
            rendered.append(item)
    return "\n".join(rendered) or None
