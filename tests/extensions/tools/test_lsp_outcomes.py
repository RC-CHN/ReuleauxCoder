from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.client import (
    LspClient,
    LspClientError,
    LspFailureFacts,
    LspRequestCancelled,
    LspRequestTimedOut,
    LspServerError,
)
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.manager import LspManager, LspTransportState
from reuleauxcoder.extensions.lsp.registry import LanguageId
from reuleauxcoder.extensions.tools.base import InterruptMode
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool


class _EmptyLspManager:
    def send_request_sync(self, *_args, **_kwargs):
        return []


class _FailingLspManager:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.cancellation = None

    def send_request_sync(self, *_args, cancellation=None, **_kwargs):
        self.cancellation = cancellation
        raise self.error


def test_lsp_success_has_full_model_content_and_compact_summary(tmp_path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tool = LspTool(lsp_manager=_EmptyLspManager())

    outcome = tool.execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.summary
    assert outcome.summary != "None"
    assert outcome.model_text
    assert outcome.metadata["operation"] == "documentSymbol"


def test_lsp_failure_has_explicit_failed_status_and_summary(tmp_path) -> None:
    missing = tmp_path / "missing.py"
    outcome = LspTool().execute(
        operation="goToDefinition",
        filePath=str(missing),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.summary
    assert "not found" in outcome.model_text.lower()


def test_lsp_tool_maps_cancellation_to_interrupted_outcome(tmp_path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = _FailingLspManager(LspRequestCancelled("request cancelled"))
    signal = threading.Event()
    tool = LspTool(lsp_manager=manager)

    with tool.execution_scope(signal):
        outcome = tool.execute(
            operation="documentSymbol",
            filePath=str(source),
            line=1,
            character=1,
        )

    assert tool.interrupt_mode is InterruptMode.CANCEL_WITH_PARTIAL
    assert manager.cancellation is signal
    assert outcome.status is ToolOutcomeStatus.CANCELLED
    assert outcome.error_kind is ToolErrorKind.INTERRUPTED
    assert "cancelled" in outcome.model_text


def test_lsp_tool_maps_timeout_to_interrupted_outcome(tmp_path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = _FailingLspManager(LspRequestTimedOut("request timed out"))
    tool = LspTool(lsp_manager=manager)

    outcome = tool.execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.TIMED_OUT
    assert outcome.error_kind is ToolErrorKind.INTERRUPTED
    assert "timed out" in outcome.model_text


@pytest.mark.parametrize(
    ("error", "expected_type"),
    (
        (LspClientError("credential=client-secret"), "LspClientError"),
        (RuntimeError("credential=runtime-secret"), "RuntimeError"),
    ),
)
def test_lsp_tool_projects_exception_types_without_untrusted_messages(
    tmp_path,
    error: Exception,
    expected_type: str,
) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    manager.send_request_sync = MagicMock(side_effect=error)  # type: ignore[method-assign]

    outcome = LspTool(lsp_manager=manager).execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "phase=request" in outcome.model_text
    assert f"error_type={expected_type}" in outcome.model_text
    assert "credential=" not in repr(outcome)


def test_lsp_tool_projects_frozen_failure_after_new_generation_is_ready(
    tmp_path,
) -> None:
    secret = "credential=server-secret-must-not-leak"
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = manager._transport_key(LanguageId.PYTHON, source)
    generation = manager._begin_transport_attempt(key, "fake-lsp")
    client = LspClient(LanguageId.PYTHON, tmp_path)
    client.stderr_capture.append(secret.encode())
    ref = manager._retain_client_stderr(key, generation, client)
    assert ref is not None
    assert manager._transition_transport(
        key,
        generation,
        LspTransportState.ERROR,
        error_type="LspServerError",
        error_phase="initialize",
        protocol_error_code=-32002,
        stderr_ref=ref,
    )
    error = manager._freeze_failure(
        LspServerError(-32002),
        key,
        phase="availability",
    )
    next_generation = manager._begin_transport_attempt(key, "replacement-lsp")
    assert next_generation == generation + 1
    assert manager._transition_transport(
        key,
        next_generation,
        LspTransportState.READY,
    )
    manager.send_request_sync = MagicMock(side_effect=error)  # type: ignore[method-assign]

    outcome = LspTool(lsp_manager=manager).execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "phase=availability" in outcome.model_text
    assert "error_type=LspServerError" in outcome.model_text
    assert "state=error" in outcome.model_text
    assert f"generation={generation}" in outcome.model_text
    assert "transport_phase=initialize" in outcome.model_text
    assert "protocol_error_code=-32002" in outcome.model_text
    assert f"stderr_ref={ref}" in outcome.model_text
    assert f"generation={next_generation}" not in outcome.model_text
    assert "replacement-lsp" not in outcome.model_text
    assert secret not in repr(outcome)


def test_lsp_tool_does_not_query_mutable_failure_projection(
    tmp_path,
) -> None:
    secret = "credential=projection-secret-must-not-leak"
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = _FailingLspManager(LspClientError(secret))

    manager.describe_failure_for_file = MagicMock(  # type: ignore[attr-defined]
        side_effect=ValueError(secret)
    )
    outcome = LspTool(lsp_manager=manager).execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "error_type=LspClientError" in outcome.model_text
    manager.describe_failure_for_file.assert_not_called()
    assert secret not in repr(outcome)


def test_lsp_tool_reports_snapshot_projection_failure_without_masking_primary(
    tmp_path,
) -> None:
    secret = "credential=snapshot-projection-must-not-leak"
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = manager._transport_key(LanguageId.PYTHON, source)
    manager._ensure_transport_status(key)
    manager._transport_status_view = MagicMock(  # type: ignore[method-assign]
        side_effect=ValueError(secret)
    )
    error = manager._freeze_failure(
        LspClientError("credential=primary-must-not-leak"),
        key,
        phase="request",
    )
    manager.send_request_sync = MagicMock(side_effect=error)  # type: ignore[method-assign]

    outcome = LspTool(lsp_manager=manager).execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "phase=request" in outcome.model_text
    assert "error_type=LspClientError" in outcome.model_text
    assert "failure_projection_error_type=ValueError" in outcome.model_text
    assert secret not in repr(outcome)
    assert "credential=primary-must-not-leak" not in repr(outcome)


def test_lsp_tool_contains_failure_render_error_and_reports_its_type(tmp_path) -> None:
    secret = "credential=render-must-not-leak"
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(LspConfig(), workspace_cwd=tmp_path)
    key = manager._transport_key(LanguageId.PYTHON, source)
    error = manager._freeze_failure(
        LspClientError("credential=primary-must-not-leak"),
        key,
        phase="request",
    )
    manager.send_request_sync = MagicMock(side_effect=error)  # type: ignore[method-assign]

    with patch.object(LspFailureFacts, "render", side_effect=ValueError(secret)):
        outcome = LspTool(lsp_manager=manager).execute(
            operation="documentSymbol",
            filePath=str(source),
            line=1,
            character=1,
        )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert "phase=request" in outcome.model_text
    assert "error_type=LspClientError" in outcome.model_text
    assert "failure_projection_error_type=ValueError" in outcome.model_text
    assert secret not in repr(outcome)
