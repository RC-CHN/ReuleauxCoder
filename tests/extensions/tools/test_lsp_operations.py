import json
from dataclasses import replace
from pathlib import Path

import pytest

from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
)
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.client import (
    LspFailureFacts,
    LspRequestCancelled,
    LspRequestTimedOut,
)
from reuleauxcoder.extensions.lsp.manager import (
    LspManager,
    LspStatusSnapshot,
    LspStatusTransportSnapshot,
    LspTransportState,
)
from reuleauxcoder.extensions.lsp.registry import LanguageId
from reuleauxcoder.extensions.tools.builtin.lsp import (
    LspDiagnosticsTool,
    LspStatusTool,
)


def _status_snapshot() -> LspStatusSnapshot:
    return LspStatusSnapshot(
        enabled=True,
        configured_languages=("python", "typescript"),
        transports=(
            LspStatusTransportSnapshot(
                language="python",
                root_hash="abc123abc123",
                state=LspTransportState.READY,
                generation=2,
            ),
            LspStatusTransportSnapshot(
                language="typescript",
                root_hash="def456def456",
                state=LspTransportState.ERROR,
                generation=1,
                error_phase="initialize",
                error_type="TimeoutError",
            ),
        ),
        availability_metrics=(("cache_hits", 4), ("resolution_attempts", 2)),
        diagnostic_batch_metrics=(("published", 3), ("stale_discarded", 1)),
    )


class _StatusManager:
    def __init__(self) -> None:
        self.health_check_calls = 0
        self.status_snapshot_calls = 0

    def health_check(self):
        self.health_check_calls += 1
        raise AssertionError("status must not probe PATH")

    def status_snapshot(self) -> LspStatusSnapshot:
        self.status_snapshot_calls += 1
        return _status_snapshot()


def test_lsp_status_reads_safe_snapshots_without_probing_or_starting() -> None:
    manager = _StatusManager()

    outcome = LspStatusTool(lsp_manager=manager).execute()

    assert outcome.success
    assert manager.health_check_calls == 0
    assert manager.status_snapshot_calls == 1
    assert json.loads(outcome.model_text) == {
        "availability_metrics": {"cache_hits": 4, "resolution_attempts": 2},
        "configured_languages": ["python", "typescript"],
        "diagnostic_batch_metrics": {"published": 3, "stale_discarded": 1},
        "enabled": True,
        "manager_bound": True,
        "transports": [
            {
                "error_phase": None,
                "error_type": None,
                "generation": 2,
                "language": "python",
                "protocol_error_code": None,
                "retry_scheduled": False,
                "return_code": None,
                "root_hash": "abc123abc123",
                "state": "ready",
            },
            {
                "error_phase": "initialize",
                "error_type": "TimeoutError",
                "generation": 1,
                "language": "typescript",
                "protocol_error_code": None,
                "retry_scheduled": False,
                "return_code": None,
                "root_hash": "def456def456",
                "state": "error",
            },
        ],
    }
    assert outcome.metadata == {
        "operation": "status",
        "manager_bound": True,
        "enabled": True,
        "configured_language_count": 2,
        "transport_count": 2,
    }
    assert "launcher" not in outcome.model_text


def test_lsp_status_reports_an_unbound_manager_as_explicit_state() -> None:
    outcome = LspStatusTool().execute()

    assert outcome.success
    assert json.loads(outcome.model_text) == {
        "manager_bound": False,
        "state": "unavailable",
    }
    assert outcome.metadata == {"operation": "status", "manager_bound": False}


@pytest.mark.parametrize(
    "invalid_projection",
    [
        object(),
        replace(_status_snapshot(), configured_languages=("typescript", "python")),
        replace(
            _status_snapshot(),
            transports=(
                replace(
                    _status_snapshot().transports[0],
                    root_hash="/private/workspace",
                ),
            ),
        ),
    ],
)
def test_lsp_status_rejects_invalid_status_projection(
    invalid_projection,
) -> None:
    manager = _StatusManager()
    manager.status_snapshot = lambda: invalid_projection

    with pytest.raises(TypeError, match="invalid projection"):
        LspStatusTool(lsp_manager=manager).execute()


def test_lsp_status_does_not_rewrite_unexpected_manager_failures() -> None:
    manager = _StatusManager()

    def fail():
        raise RuntimeError("manager-internal-secret")

    manager.status_snapshot = fail

    with pytest.raises(RuntimeError, match="manager-internal-secret"):
        LspStatusTool(lsp_manager=manager).execute()


def test_lsp_status_schema_is_closed_and_argument_free() -> None:
    assert LspStatusTool.effect_class == "read_only_internal"
    assert LspStatusTool.parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


class _DiagnosticsManager:
    def __init__(self, outcome: DiagnosticOutcome | BaseException) -> None:
        self.outcome = outcome
        self.path = None
        self.cancellation = None
        self.acknowledged = None

    def request_diagnostics_sync(self, path, *, cancellation=None):
        self.path = path
        self.cancellation = cancellation
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def acknowledge_diagnostic_batch(self, batch_id, *, consumer_id):
        self.acknowledged = (batch_id, consumer_id)
        return True


def _diagnostic_outcome(path: Path, *, clean: bool = False) -> DiagnosticOutcome:
    block = DiagnosticBlock(
        file_path=path.name,
        items=(
            []
            if clean
            else [Diagnostic(line=2, character=3, message="broken <value>")]
        ),
    )
    return DiagnosticOutcome(
        batch_id="batch-1",
        route=DiagnosticRoute(file_path=path),
        request_sequence=1,
        status=(
            DiagnosticOutcomeStatus.PUBLISHED_CLEAN
            if clean
            else DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY
        ),
        created_at=1.0,
        document_version=4,
        diagnostic_generation=7,
        block=block,
    )


@pytest.mark.parametrize("clean", [False, True])
def test_lsp_diagnostics_renders_and_acknowledges_typed_publish(
    tmp_path: Path,
    clean: bool,
) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = _DiagnosticsManager(_diagnostic_outcome(source, clean=clean))

    outcome = LspDiagnosticsTool(lsp_manager=manager).execute(filePath=str(source))

    assert outcome.success
    assert manager.path == source
    assert manager.acknowledged == ("batch-1", "lsp_diagnostics")
    assert outcome.metadata == {
        "operation": "diagnostics",
        "diagnostic_status": (
            "published_clean" if clean else "published_nonempty"
        ),
        "batch_id": "batch-1",
        "diagnostic_count": 0 if clean else 1,
        "document_version": 4,
        "diagnostic_generation": 7,
        "acknowledged": True,
    }
    if clean:
        assert "published clean" in outcome.model_text
    else:
        assert "ERROR [2:3] broken &lt;value&gt;" in outcome.model_text


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LspRequestCancelled("cancelled"), "cancelled"),
        (LspRequestTimedOut("timed out"), "timed_out"),
    ],
)
def test_lsp_diagnostics_preserves_waiter_interrupt_status(
    tmp_path: Path,
    error: BaseException,
    status: str,
) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = _DiagnosticsManager(error)

    outcome = LspDiagnosticsTool(lsp_manager=manager).execute(filePath=str(source))

    assert outcome.status.value == status
    assert outcome.error_kind.value == "interrupted"
    assert manager.acknowledged is None


def test_lsp_diagnostics_preserves_typed_server_failure(tmp_path: Path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    terminal = DiagnosticOutcome(
        batch_id="failed-batch",
        route=DiagnosticRoute(file_path=source),
        request_sequence=1,
        status=DiagnosticOutcomeStatus.SERVER_UNAVAILABLE,
        created_at=1.0,
        failure=LspFailureFacts(
            phase="availability",
            error_type="LspServerUnavailable",
        ),
    )
    manager = _DiagnosticsManager(terminal)

    outcome = LspDiagnosticsTool(lsp_manager=manager).execute(filePath=str(source))

    assert not outcome.success
    assert "status=server_unavailable" in outcome.model_text
    assert "error_type=LspServerUnavailable" in outcome.model_text
    assert manager.acknowledged == ("failed-batch", "lsp_diagnostics")


class _CountingLock:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.entries = 0

    def __enter__(self):
        self.inner.acquire()
        self.entries += 1
        return self

    def __exit__(self, exc_type, exc, traceback):  # noqa: ARG002
        self.inner.release()


class _UncommittedClientFailure:
    @property
    def transport_failure_reason(self):
        raise AssertionError("status snapshot must not inspect client failure state")

    @property
    def transport_failure_returncode(self):
        raise AssertionError("status snapshot must not inspect client return code")


def test_manager_status_snapshot_uses_only_committed_transport_state(
    tmp_path: Path,
) -> None:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = (LanguageId.PYTHON, tmp_path)
    with manager._lock:
        manager._record_transport_status_locked(
            key,
            state=LspTransportState.READY,
            generation=4,
            launcher="configured-launcher",
        )
        manager._transports[key] = _UncommittedClientFailure()

    snapshot = manager.status_snapshot()

    assert snapshot.transports == (
        LspStatusTransportSnapshot(
            language="python",
            root_hash=manager._workspace_identifier(tmp_path),
            state=LspTransportState.READY,
            generation=4,
        ),
    )


def test_real_manager_status_snapshot_is_atomic_private_and_side_effect_free(
    tmp_path: Path,
) -> None:
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    workspace_root = tmp_path / "private-workspace"
    file_path = workspace_root / "credential-file.py"
    key = (LanguageId.PYTHON, workspace_root)
    with manager._lock:
        manager._record_transport_status_locked(
            key,
            state=LspTransportState.ERROR,
            generation=3,
            launcher="/private/bin/credential-launcher",
            error_type="InitializeError",
            error_phase="initialize",
            protocol_error_code=-32002,
            return_code=23,
            stderr_ref="stderr-secret-reference",
            retry_at=1234.5,
        )
        batch = DiagnosticBatch(
            batch_id="expired-private-batch",
            route=DiagnosticRoute(file_path=file_path),
            request_sequence=1,
            document_version=1,
            diagnostic_generation=1,
            block=DiagnosticBlock(file_path=str(file_path), items=[]),
            created_at=0.0,
        )
        manager._diagnostic_batches[batch.batch_id] = batch
        manager._negative_availability_until[
            (key, "/private/bin/credential-launcher")
        ] = 10_000.0

    manager._diagnostic_clock = lambda: 10_000.0
    manager._command_lookup = lambda command: (_ for _ in ()).throw(
        AssertionError(f"status probed launcher: {command}")
    )
    manager.start_worker = lambda: (_ for _ in ()).throw(
        AssertionError("status started the worker")
    )
    counting_lock = _CountingLock(manager._lock)
    manager._lock = counting_lock

    before = (
        dict(manager._transport_statuses),
        tuple(manager._transport_state_history),
        dict(manager._availability),
        dict(manager._negative_availability_until),
        dict(manager._availability_metrics),
        dict(manager._diagnostic_batches),
        dict(manager._diagnostic_failure_outcomes),
        set(manager._pending_diagnostic_requests),
        dict(manager._diagnostic_batch_metrics),
        dict(manager._stderr_records),
        manager._worker_thread,
        manager._accepting_work,
    )

    snapshot = manager.status_snapshot()
    outcome = LspStatusTool(lsp_manager=manager).execute()

    after = (
        dict(manager._transport_statuses),
        tuple(manager._transport_state_history),
        dict(manager._availability),
        dict(manager._negative_availability_until),
        dict(manager._availability_metrics),
        dict(manager._diagnostic_batches),
        dict(manager._diagnostic_failure_outcomes),
        set(manager._pending_diagnostic_requests),
        dict(manager._diagnostic_batch_metrics),
        dict(manager._stderr_records),
        manager._worker_thread,
        manager._accepting_work,
    )
    assert before == after
    assert counting_lock.entries == 2
    assert "expired-private-batch" in manager._diagnostic_batches
    assert manager._diagnostic_batch_metrics["expired"] == 0

    rendered = repr(snapshot) + outcome.model_text
    assert str(workspace_root) not in rendered
    assert str(file_path) not in rendered
    assert "credential-launcher" not in rendered
    assert "stderr-secret-reference" not in rendered
    assert "launcher" not in outcome.model_text
    assert "stderr" not in outcome.model_text
    payload = json.loads(outcome.model_text)
    assert payload["transports"] == [
        {
            "error_phase": "initialize",
            "error_type": "InitializeError",
            "generation": 3,
            "language": "python",
            "protocol_error_code": -32002,
            "retry_scheduled": True,
            "return_code": 23,
            "root_hash": manager._workspace_identifier(workspace_root),
            "state": "error",
        }
    ]
