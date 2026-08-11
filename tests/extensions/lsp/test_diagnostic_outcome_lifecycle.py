from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from reuleauxcoder.extensions.lsp.client import LspDocumentReadError, LspFailureFacts
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
)
from reuleauxcoder.extensions.lsp.manager import (
    MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER,
    LspManager,
)
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _manager(tmp_path: Path) -> LspManager:
    manager = LspManager(
        LspConfig(enabled=True, poll_timeout_ms=1),
        workspace_cwd=tmp_path,
    )
    manager.start_worker = MagicMock()  # type: ignore[method-assign]
    manager._accepting_work = True
    manager._availability[LanguageId.PYTHON] = True
    manager._diagnostic_clock = lambda: 100.0
    return manager


def _request(manager: LspManager, path: Path, *, route: DiagnosticRoute | None = None):
    batch_id = manager.enqueue_diagnostics(path, route=route)
    assert batch_id is not None
    request = manager._diagnostics_queue.pop()
    return batch_id, request


def _server(*, baseline: int, current: int, diagnostics: list[Diagnostic]) -> MagicMock:
    server = MagicMock()
    server.diagnostics_generation.side_effect = [baseline, current]
    server.diagnostic_document_version.return_value = 3
    server.did_open = AsyncMock()
    server.did_change = AsyncMock()
    server.did_save = AsyncMock()
    server.refresh_diagnostics = AsyncMock()
    server.wait_for_diagnostics = AsyncMock(return_value=diagnostics)
    return server


def _old_batch(path: Path, route: DiagnosticRoute) -> DiagnosticBatch:
    return DiagnosticBatch(
        batch_id="old-published",
        route=route,
        request_sequence=0,
        document_version=1,
        diagnostic_generation=1,
        block=DiagnosticBlock(
            file_path=path.name,
            items=[Diagnostic(line=1, character=1, message="old error")],
        ),
        created_at=1.0,
    )


def test_server_unavailable_is_agent_consumable_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    route = DiagnosticRoute(file_path=path, agent_id="agent", session_id="session")
    old = _old_batch(path, route)
    manager._diagnostic_batches[old.batch_id] = old
    manager._get_or_create_server = AsyncMock(return_value=None)
    batch_id, request = _request(manager, path, route=route)

    asyncio.run(manager._handle_diagnostics_request(request))

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.SERVER_UNAVAILABLE
    assert outcome.failure is not None
    assert outcome.failure.phase == "availability"
    assert outcome.failure.error_type == "LspServerUnavailable"
    assert manager.diagnostic_request_result(batch_id) == ()
    assert manager.pending_diagnostic_batches() == (old,)
    assert manager.diagnostic_batch_metrics()["outcome_server_unavailable"] == 1


def test_timeout_is_not_faked_as_a_clean_publish(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    server = _server(baseline=4, current=4, diagnostics=[])
    manager._get_or_create_server = AsyncMock(return_value=server)
    batch_id, request = _request(manager, path)

    asyncio.run(manager._handle_diagnostics_request(request))

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.TIMED_OUT
    assert outcome.block is None
    assert outcome.failure is not None
    assert outcome.failure.phase == "diagnostics_wait"
    assert outcome.failure.error_type == "LspRequestTimedOut"
    assert manager.pending_diagnostic_batches(batch_id=batch_id) == ()


def test_document_read_failure_retains_previous_published_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    route = DiagnosticRoute(file_path=path, agent_id="agent", session_id="session")
    old = _old_batch(path, route)
    manager._diagnostic_batches[old.batch_id] = old
    manager._get_or_create_server = AsyncMock(
        return_value=_server(baseline=1, current=2, diagnostics=[])
    )
    manager._load_document_for_sync = MagicMock(  # type: ignore[method-assign]
        side_effect=LspDocumentReadError("private path must not escape")
    )
    batch_id, request = _request(manager, path, route=route)

    asyncio.run(manager._handle_diagnostics_request(request))

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.ERROR
    assert outcome.failure is not None
    assert outcome.failure.phase == "document_sync"
    assert outcome.failure.error_type == "LspDocumentReadError"
    assert manager.pending_diagnostic_batches() == (old,)
    assert "private path" not in outcome.failure.render()


def test_only_a_new_publish_replaces_previous_document_state(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    route = DiagnosticRoute(file_path=path, agent_id="agent", session_id="session")
    old = _old_batch(path, route)
    manager._diagnostic_batches[old.batch_id] = old
    manager._get_or_create_server = AsyncMock(
        return_value=_server(baseline=1, current=2, diagnostics=[])
    )
    batch_id, request = _request(manager, path, route=route)

    asyncio.run(manager._handle_diagnostics_request(request))

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.PUBLISHED_CLEAN
    assert outcome.block is not None and outcome.block.is_empty()
    assert [batch.batch_id for batch in manager.pending_diagnostic_batches()] == [
        batch_id
    ]
    assert manager.diagnostic_batch_metrics()["overwritten"] == 1


def test_runtime_event_observer_failure_cannot_rewrite_published_outcome(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    manager._get_or_create_server = AsyncMock(
        return_value=_server(
            baseline=1,
            current=2,
            diagnostics=[Diagnostic(line=1, character=1, message="broken")],
        )
    )
    manager._publish_diagnostic_event = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("secondary observer failure")
    )
    batch_id, request = _request(manager, path)

    asyncio.run(manager._handle_diagnostics_request(request))

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY
    assert outcome.block is not None
    assert [item.message for item in outcome.block.items] == ["broken"]


def test_superseded_queued_request_gets_exact_stale_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    first_id = manager.enqueue_diagnostics(path)
    second_id = manager.enqueue_diagnostics(path)
    assert first_id is not None and second_id is not None

    first = manager.diagnostic_request_outcome(first_id)
    assert first is not None
    assert first.status is DiagnosticOutcomeStatus.STALE_DISCARDED
    assert manager.diagnostic_request_result(first_id) == ()
    assert manager.diagnostic_request_outcome(second_id) is None
    assert manager.diagnostic_request_result(second_id) is None
    assert manager.diagnostic_batch_metrics()["outcome_stale_discarded"] == 1


def test_terminal_transition_is_exactly_once_under_manager_lock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    batch_id, request = _request(manager, path)
    failure = LspFailureFacts(phase="document_sync", error_type="LspClientError")

    with manager._lock:
        assert manager._complete_diagnostic_request_locked(
            request,
            status=DiagnosticOutcomeStatus.ERROR,
            failure=failure,
        )
        assert not manager._complete_diagnostic_request_locked(
            request,
            status=DiagnosticOutcomeStatus.CANCELLED,
            failure=failure,
        )

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.ERROR
    assert manager.diagnostic_batch_metrics()["outcome_error"] == 1
    assert manager.diagnostic_batch_metrics()["outcome_cancelled"] == 0


def test_acknowledgement_claims_failure_outcome_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    manager._get_or_create_server = AsyncMock(return_value=None)
    batch_id, request = _request(manager, path)
    asyncio.run(manager._handle_diagnostics_request(request))

    assert manager.acknowledge_diagnostic_batch(batch_id, consumer_id="agent")
    assert not manager.acknowledge_diagnostic_batch(batch_id, consumer_id="other")
    assert manager.diagnostic_request_outcome(batch_id) is None
    assert manager.diagnostic_batch_acknowledgement(batch_id) == "agent"


def test_shutdown_cancels_queued_diagnostics_without_losing_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    batch_id = manager.enqueue_diagnostics(path)
    assert batch_id is not None

    assert manager.shutdown_all(timeout=1.0)

    outcome = manager.diagnostic_request_outcome(batch_id)
    assert outcome is not None
    assert outcome.status is DiagnosticOutcomeStatus.CANCELLED
    assert outcome.failure is not None
    assert outcome.failure.phase == "shutdown"


def test_failure_backlog_cannot_capacity_evict_last_published_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "main.py"
    manager = _manager(tmp_path)
    route = DiagnosticRoute(file_path=path, agent_id="agent", session_id="session")
    old = _old_batch(path, route)
    manager._diagnostic_batches[old.batch_id] = old

    with manager._lock:
        for index in range(MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER):
            manager._store_diagnostic_failure_outcome_locked(
                DiagnosticOutcome(
                    batch_id=f"failure-{index}",
                    route=route,
                    request_sequence=index + 1,
                    status=DiagnosticOutcomeStatus.ERROR,
                    created_at=10.0 + index,
                    failure=LspFailureFacts(
                        phase="diagnostics_wait",
                        error_type="LspClientError",
                    ),
                )
            )

    assert manager.pending_diagnostic_batches() == (old,)
    failures = manager.pending_diagnostic_failure_outcomes_for_owner(
        agent_id="agent",
        session_generation=None,
        session_id="session",
    )
    assert len(failures) == MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER - 1
    assert all(outcome.batch_id != "failure-0" for outcome in failures)
    assert manager.diagnostic_batch_metrics()["capacity_evicted"] == 1
