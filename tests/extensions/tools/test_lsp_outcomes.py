from __future__ import annotations

import threading

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.client import (
    LspRequestCancelled,
    LspRequestTimedOut,
)
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
