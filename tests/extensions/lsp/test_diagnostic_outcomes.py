from __future__ import annotations

from pathlib import Path

import pytest

from reuleauxcoder.extensions.lsp.client import LspFailureFacts
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
    render_diagnostic_outcome,
    render_diagnostic_outcomes,
)
from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
)


def _route() -> DiagnosticRoute:
    return DiagnosticRoute(
        file_path=Path("/workspace/main.py"),
        agent_id="agent",
        session_generation=2,
        session_id="session",
        turn_id="turn",
        tool_call_id="edit",
    )


def _failure_outcome(
    status: DiagnosticOutcomeStatus = DiagnosticOutcomeStatus.ERROR,
) -> DiagnosticOutcome:
    return DiagnosticOutcome(
        batch_id="failure",
        route=_route(),
        request_sequence=1,
        status=status,
        created_at=10.0,
        failure=LspFailureFacts(
            phase="document_sync",
            error_type="LspDocumentReadError",
            language="python",
            root_hash="abc123",
        ),
    )


def test_published_batch_projects_to_typed_nonempty_outcome() -> None:
    block = DiagnosticBlock(
        file_path="main.py",
        items=[Diagnostic(line=1, character=1, message="broken")],
    )
    outcome = DiagnosticOutcome.from_batch(
        DiagnosticBatch(
            batch_id="published",
            route=_route(),
            request_sequence=3,
            document_version=4,
            diagnostic_generation=5,
            block=block,
            created_at=12.0,
        )
    )

    assert outcome.status is DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY
    assert outcome.block is block
    assert outcome.is_published
    assert render_diagnostic_outcome(outcome) is None


def test_published_clean_and_nonempty_invariants_are_strict() -> None:
    with pytest.raises(ValueError, match="published-clean"):
        DiagnosticOutcome(
            batch_id="invalid-clean",
            route=_route(),
            request_sequence=1,
            status=DiagnosticOutcomeStatus.PUBLISHED_CLEAN,
            created_at=1.0,
            document_version=1,
            diagnostic_generation=1,
            block=DiagnosticBlock(
                file_path="main.py",
                items=[Diagnostic(line=1, character=1, message="broken")],
            ),
        )

    with pytest.raises(ValueError, match="published-nonempty"):
        DiagnosticOutcome(
            batch_id="invalid-nonempty",
            route=_route(),
            request_sequence=1,
            status=DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY,
            created_at=1.0,
            document_version=1,
            diagnostic_generation=1,
            block=DiagnosticBlock(file_path="main.py"),
        )


def test_failure_outcome_cannot_claim_a_diagnostic_block() -> None:
    with pytest.raises(ValueError, match="non-published"):
        DiagnosticOutcome(
            batch_id="invalid-failure",
            route=_route(),
            request_sequence=1,
            status=DiagnosticOutcomeStatus.ERROR,
            created_at=1.0,
            block=DiagnosticBlock(file_path="main.py"),
        )


def test_failure_render_contains_only_safe_frozen_facts() -> None:
    rendered = render_diagnostic_outcome(_failure_outcome())

    assert rendered is not None
    assert "status=error" in rendered
    assert "phase=document_sync" in rendered
    assert "error_type=LspDocumentReadError" in rendered
    assert "/workspace" not in rendered


def test_stale_outcome_is_visible_without_faking_an_error() -> None:
    outcome = DiagnosticOutcome(
        batch_id="stale",
        route=_route(),
        request_sequence=2,
        status=DiagnosticOutcomeStatus.STALE_DISCARDED,
        created_at=1.0,
    )

    assert render_diagnostic_outcome(outcome) == (
        "LSP diagnostics ended (status=stale_discarded)"
    )


def test_projection_failure_is_contained_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _failure_outcome()

    def fail_projection(_outcome: DiagnosticOutcome) -> str:
        raise RuntimeError("do not expose this message")

    monkeypatch.setattr(
        "reuleauxcoder.extensions.lsp.diagnostic_outcomes.render_diagnostic_outcome",
        fail_projection,
    )

    rendered = render_diagnostic_outcomes((outcome,))
    assert rendered is not None
    assert "failure_projection_error_type=RuntimeError" in rendered
    assert "do not expose" not in rendered
