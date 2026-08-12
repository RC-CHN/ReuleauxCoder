"""Tests for LSP hook integration.

Tests the LspEditObserverHook (AFTER_TOOL_EXECUTE) and
LspDiagnosticsInjectorHook (BEFORE_LLM_REQUEST) with mocked LspManager.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import (
    EDIT_TOOLS,
    LspEditObserverHook,
    _extract_file_path,
)
from reuleauxcoder.domain.hooks.builtin.lsp_injector import (
    LspDiagnosticsInjectorHook,
)
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import (
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
)
from reuleauxcoder.extensions.lsp.manager import LspManager


def _make_manager() -> LspManager:
    """Create an LspManager with all languages marked unavailable."""
    config = LspConfig(enabled=True)
    mgr = LspManager(config, workspace_cwd=Path("/tmp"))
    # Hook unit tests control completion directly and must never start a real
    # language-server process in the background.
    mgr.start_worker = MagicMock()  # type: ignore[method-assign]
    mgr._accepting_work = True
    for lang in range(10):  # all LanguageId values
        mgr._availability[lang] = False
    return mgr


def _publish_batch(
    mgr: LspManager,
    block: DiagnosticBlock,
    *,
    batch_id: str,
    route: DiagnosticRoute | None = None,
) -> DiagnosticBatch:
    route = route or DiagnosticRoute(file_path=Path(block.file_path))
    batch = DiagnosticBatch(
        batch_id=batch_id,
        route=route,
        request_sequence=1,
        document_version=1,
        diagnostic_generation=1,
        block=block,
    )
    with mgr._lock:
        mgr._diagnostic_batches[batch_id] = batch
    return batch


def _complete_enqueued_batch(mgr: LspManager, block: DiagnosticBlock) -> None:
    """Make the edit hook observe a worker result for its exact request ID."""

    def enqueue(file_path: Path, *, route=None, document_committed=False):
        assert route is not None
        assert document_committed is True
        batch_id = f"batch-{route.tool_call_id}"
        _publish_batch(mgr, block, batch_id=batch_id, route=route)
        return batch_id

    mgr.enqueue_diagnostics = MagicMock(side_effect=enqueue)  # type: ignore[method-assign]


def _execution_state_tail() -> dict:
    return {
        "role": "user",
        "content": (
            '<execution_state plan_revision="0">\n'
            '<execution_data trust="untrusted_data">\n{}\n</execution_data>\n'
            "<runtime_instruction>Continue.</runtime_instruction>\n"
            "</execution_state>"
        ),
    }


# === LspEditObserverHook ===


class TestExtractFilePath:
    def test_edit_file(self) -> None:
        assert (
            _extract_file_path("edit_file", {"file_path": "src/main.py"})
            == "src/main.py"
        )

    def test_write_file(self) -> None:
        assert (
            _extract_file_path("write_file", {"file_path": "/tmp/out.py"})
            == "/tmp/out.py"
        )

    def test_missing_key(self) -> None:
        assert _extract_file_path("edit_file", {}) is None


class TestLspEditObserverBasic:
    def test_returns_early_when_manager_none(self) -> None:
        hook = LspEditObserverHook(lsp_manager=None)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1", name="edit_file", arguments={"file_path": "x.py"}
            ),
        )
        # Should not raise
        hook.run(context)

    def test_returns_early_when_manager_disabled(self) -> None:
        config = LspConfig(enabled=False)
        mgr = LspManager(config, workspace_cwd=Path("/tmp"))
        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1", name="edit_file", arguments={"file_path": "x.py"}
            ),
        )
        # Should not enqueue
        hook.run(context)
        assert len(mgr._diagnostics_queue) == 0

    def test_returns_early_when_no_tool_call(self) -> None:
        mgr = _make_manager()
        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=None,
        )
        hook.run(context)

    def test_returns_early_for_non_edit_tools(self) -> None:
        mgr = _make_manager()
        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1", name="read_file", arguments={"file_path": "x.py"}
            ),
        )
        hook.run(context)
        assert len(mgr._diagnostics_queue) == 0

    def test_enqueues_diagnostics_for_edit_tools(self) -> None:
        mgr = _make_manager()
        mgr.diagnostic_request_result = MagicMock(  # type: ignore[method-assign]
            return_value=()
        )
        # Mark Python as available so enqueue passes the guard
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="edited"),
            round_index=1,
        )
        hook.run(context)

    def test_enqueues_atomic_document_commit_for_edit_tools(self) -> None:
        mgr = _make_manager()
        mgr.diagnostic_request_result = MagicMock(  # type: ignore[method-assign]
            return_value=()
        )
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="write_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="wrote"),
        )
        hook.run(context)
        assert len(mgr._diagnostics_queue) == 1
        request = mgr._diagnostics_queue[0]
        assert request.route.file_path == Path("/tmp/test.py").resolve()
        assert request.document_committed is True

    def test_missing_launcher_completes_without_waiting_for_poll_deadline(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "test.py"
        path.write_text("value = 1\n", encoding="utf-8")
        mgr = LspManager(LspConfig(enabled=True), workspace_cwd=tmp_path)
        mgr._command_lookup = MagicMock(return_value=None)
        batch_ids: list[str] = []
        enqueue = mgr.enqueue_diagnostics

        def capture_enqueue(*args, **kwargs):
            batch_id = enqueue(*args, **kwargs)
            assert batch_id is not None
            batch_ids.append(batch_id)
            return batch_id

        mgr.enqueue_diagnostics = MagicMock(  # type: ignore[method-assign]
            side_effect=capture_enqueue
        )
        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="missing-launcher",
                name="edit_file",
                arguments={"file_path": str(path)},
            ),
            outcome=ToolOutcome(content="edited"),
        )

        started_at = time.monotonic()
        try:
            result = hook.run(context)
            elapsed = time.monotonic() - started_at
        finally:
            assert mgr.shutdown_all(timeout=1.0)

        assert result is context
        assert elapsed < 1.0
        assert len(batch_ids) == 1
        assert mgr.diagnostic_request_result(batch_ids[0]) == ()
        mgr._command_lookup.assert_called_once()

    def test_uses_resolved_outcome_path_for_document_commit(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "reuleauxcoder.domain.hooks.builtin.lsp_edit_observer._DIAGNOSTICS_POLL_DEADLINE",
            0,
        )
        mgr = _make_manager()
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "relative.py"},
            ),
            outcome=ToolOutcome(
                content="edited",
                metadata={"resolved_path": "/tmp/canonical.py"},
            ),
        )

        hook.run(context)

        assert len(mgr._diagnostics_queue) == 1
        assert mgr._diagnostics_queue[0].route.file_path == Path(
            "/tmp/canonical.py"
        ).resolve()

    def test_failed_edit_does_not_notify_or_enqueue(self) -> None:
        mgr = _make_manager()
        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="failed",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content="edit failed",
                error_kind=ToolErrorKind.EXECUTION,
            ),
        )

        hook.run(context)

        assert len(mgr._diagnostics_queue) == 0

    def test_all_edit_tools_are_handled(self) -> None:
        """Verify that the EDIT_TOOLS set covers all expected edit tools."""
        for tool_name in EDIT_TOOLS:
            assert _extract_file_path(tool_name, {"file_path": "f.py"}) == "f.py"


class TestLspEditObserverCreateFromConfig:
    def test_create_from_config(self) -> None:
        hook = LspEditObserverHook.create_from_config(MagicMock())
        assert hook.lsp_manager is None
        assert hook.name == "lsp_edit_observer"

    def test_bind_lsp_manager_service(self) -> None:
        hook = LspEditObserverHook.create_from_config(MagicMock())
        mgr = _make_manager()
        hook.bind_runtime_service("lsp_manager", mgr)
        assert hook.lsp_manager is mgr

        hook.bind_runtime_service("lsp_manager", None)
        assert hook.lsp_manager is None

    def test_subagent_clone_does_not_share_manager(self) -> None:
        mgr = _make_manager()
        hook = LspEditObserverHook(lsp_manager=mgr)

        cloned = hook.clone_for_scope("subagent")

        assert cloned is not hook
        assert cloned.lsp_manager is None


# === LspDiagnosticsInjectorHook ===


class TestLspDiagnosticsInjectorBasic:
    def test_returns_same_context_when_manager_none(self) -> None:
        hook = LspDiagnosticsInjectorHook(lsp_manager=None)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[],
        )
        result = hook.run(context)
        assert result is context
        assert len(result.messages) == 0

    def test_returns_same_context_when_no_blocks(self) -> None:
        mgr = _make_manager()
        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[],
        )
        result = hook.run(context)
        assert result is context
        assert len(result.messages) == 0

    def test_returns_same_context_when_disabled(self) -> None:
        config = LspConfig(enabled=False)
        mgr = LspManager(config, workspace_cwd=Path("/tmp"))
        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[{"role": "user", "content": "hello"}],
        )
        result = hook.run(context)
        assert len(result.messages) == 1  # unchanged

    def test_injects_diagnostics_message(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        _publish_batch(mgr, block, batch_id="batch-1")

        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )
        result = hook.run(context)

        # Diagnostics are appended inside the request-time execution overlay,
        # before the trusted runtime instruction.
        assert len(result.messages) == 1
        assert "err" in result.messages[0]["content"]
        assert "[LSP DIAGNOSTICS]" in result.messages[0]["content"]
        assert result.messages[0]["content"].index("err") < result.messages[0][
            "content"
        ].index("<runtime_instruction>")
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-1"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-1") is None

        assert context._commit_dispatch_callbacks() == ()
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-1") == (
            "lsp-inject:unknown:unknown:unknown"
        )

    def test_only_atomic_ack_winner_keeps_diagnostics_in_wire_payload(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="test.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
            batch_id="batch-race",
        )
        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        winner = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            turn_id="winner",
            messages=[_execution_state_tail()],
        )
        loser = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            turn_id="loser",
            messages=[_execution_state_tail()],
        )

        hook.run(winner)
        hook.run(loser)
        assert "[LSP DIAGNOSTICS]" in winner.messages[0]["content"]
        assert "[LSP DIAGNOSTICS]" in loser.messages[0]["content"]

        assert winner._commit_dispatch_callbacks() == ()
        assert loser._commit_dispatch_callbacks() == ()

        assert "[LSP DIAGNOSTICS]" in winner.messages[0]["content"]
        assert "[LSP DIAGNOSTICS]" not in loser.messages[0]["content"]
        assert mgr.diagnostic_batch_acknowledgement("batch-race") == (
            "lsp-inject:unknown:unknown:winner"
        )

    def test_invalid_tail_does_not_consume_diagnostics(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        _publish_batch(mgr, block, batch_id="batch-1")

        assert len(mgr.pending_diagnostic_batches()) == 1

        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[],
        )
        hook.run(context)
        assert len(mgr.pending_diagnostic_batches()) == 1
        assert mgr.diagnostic_batch_acknowledgement("batch-1") is None

    def test_later_transform_removal_does_not_ack_diagnostics(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="test.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
            batch_id="batch-removed",
        )
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )

        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(context)
        context.messages[:] = [_execution_state_tail()]

        assert context._commit_dispatch_callbacks() == ()
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-removed"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-removed") is None


class TestLspDiagnosticsInjectorCreateFromConfig:
    def test_create_from_config(self) -> None:
        hook = LspDiagnosticsInjectorHook.create_from_config(MagicMock())
        assert hook.lsp_manager is None
        assert hook.name == "lsp_diagnostics_injector"

    def test_bind_lsp_manager_service(self) -> None:
        hook = LspDiagnosticsInjectorHook.create_from_config(MagicMock())
        mgr = _make_manager()
        hook.bind_runtime_service("lsp_manager", mgr)
        assert hook.lsp_manager is mgr

    def test_subagent_clone_does_not_share_manager(self) -> None:
        mgr = _make_manager()
        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)

        cloned = hook.clone_for_scope("subagent")

        assert cloned is not hook
        assert cloned.lsp_manager is None


# === LspEditObserverHook document-scoped consumption ===


class TestLspEditObserverDedup:
    def test_consumes_only_edited_file_after_injecting_diagnostics(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            DiagnosticBlock,
        )
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        mgr = _make_manager()
        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        block = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        _complete_enqueued_batch(mgr, block)

        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="edited"),
            round_index=1,
        )
        hook.run(context)

        assert context.outcome is not None
        assert [item.message for item in context.outcome.diagnostics] == ["err"]
        assert "err" in context.outcome.model_text
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-1") == "lsp-edit:1"

    def test_empty_diagnostics_leave_no_results(self) -> None:
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        mgr = _make_manager()
        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        hook = LspEditObserverHook(lsp_manager=mgr)
        _complete_enqueued_batch(
            mgr,
            DiagnosticBlock(file_path="/tmp/test.py", items=[]),
        )
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="edited"),
            round_index=1,
        )
        hook.run(context)
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-1") == "lsp-edit:1"

    def test_does_not_consume_other_file_batch(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            DiagnosticBlock,
        )

        mgr = _make_manager()
        edited = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="edited")],
        )
        other = DiagnosticBlock(
            file_path="/tmp/other.py",
            items=[Diagnostic(line=1, character=1, message="other")],
        )
        _complete_enqueued_batch(mgr, edited)
        _publish_batch(mgr, other, batch_id="batch-other")

        hook = LspEditObserverHook(lsp_manager=mgr)
        tool_context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="edited"),
            round_index=1,
        )
        hook.run(tool_context)

        assert tool_context.outcome is not None
        assert [item.message for item in tool_context.outcome.diagnostics] == ["edited"]
        remaining = mgr.pending_diagnostic_batches()
        assert [batch.block.file_path for batch in remaining] == ["/tmp/other.py"]

    def test_ui_failure_does_not_crash_delivered_diagnostics(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        ui_bus = MagicMock()
        ui_bus.info.side_effect = RuntimeError("ui failed")
        mgr = _make_manager()
        mgr.ui_bus = ui_bus
        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True
        _complete_enqueued_batch(
            mgr,
            DiagnosticBlock(
                file_path="/tmp/test.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
        )
        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="edited"),
        )

        hook.run(context)

        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-1") == "lsp-edit:1"

    def test_ack_failure_keeps_projected_batch_retryable_and_secret_safe(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        mgr = _make_manager()
        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True
        _complete_enqueued_batch(
            mgr,
            DiagnosticBlock(
                file_path="/tmp/test.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
        )
        mgr.acknowledge_diagnostic_batches = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("SENTINEL_ACK_SECRET")
        )
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            outcome=ToolOutcome(content="edited"),
        )

        LspEditObserverHook(lsp_manager=mgr).run(context)

        assert context.outcome is not None
        assert [item.message for item in context.outcome.diagnostics] == ["err"]
        assert "err" in context.outcome.model_text
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-1"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-1") is None
        mgr.acknowledge_diagnostic_batches.assert_called_once_with(
            ("batch-1",),
            consumer_id="lsp-edit:1",
        )
        assert "error_type=RuntimeError" in caplog.text
        assert "SENTINEL_ACK_SECRET" not in caplog.text


# === LspDiagnosticsInjectorHook scoped dedup ===


class TestLspDiagnosticsInjectorDedup:
    def test_injector_keeps_new_unrelated_batch(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="/tmp/other.py",
            items=[Diagnostic(line=1, character=1, message="other")],
        )
        _publish_batch(mgr, block, batch_id="batch-other")

        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )
        result = hook.run(context)

        assert "[LSP DIAGNOSTICS]" in result.messages[0]["content"]
        assert "other" in result.messages[0]["content"]

    def test_injects_available_batch(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            DiagnosticBlock,
        )

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        _publish_batch(mgr, block, batch_id="batch-1")

        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )
        result = hook.run(context)

        # Injection happened
        assert "[LSP DIAGNOSTICS]" in result.messages[0]["content"]
        assert "err" in result.messages[0]["content"]
        # Transforming the payload alone does not claim the batch.
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-1"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-1") is None

        assert context._commit_dispatch_callbacks() == ()
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-1") == (
            "lsp-inject:unknown:unknown:unknown"
        )

    def test_carries_prior_turn_batch_for_exact_session_owner(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="late")],
        )
        _publish_batch(
            mgr,
            block,
            batch_id="batch-late",
            route=DiagnosticRoute(
                file_path=Path("/tmp/test.py"),
                agent_id="parent",
                session_generation=2,
                session_id="session",
                turn_id="turn-1",
                tool_call_id="edit-1",
            ),
        )
        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
            agent_id="parent",
            session_generation=2,
            session_id="session",
            turn_id="turn-2",
        )

        hook.run(context)

        assert "late" in context.messages[0]["content"]
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-late"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-late") is None
        assert mgr.diagnostic_batch_metrics()["carried_forward"] == 0

        assert context._commit_dispatch_callbacks() == ()
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-late") == (
            "lsp-inject:parent:2:turn-2"
        )
        assert mgr.diagnostic_batch_metrics()["carried_forward"] == 1

    def test_different_agent_generation_or_session_never_crosses_owner(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        routes = (
            DiagnosticRoute(
                file_path=Path("/tmp/agent.py"),
                agent_id="child",
                session_generation=2,
                session_id="session",
            ),
            DiagnosticRoute(
                file_path=Path("/tmp/generation.py"),
                agent_id="parent",
                session_generation=1,
                session_id="session",
            ),
            DiagnosticRoute(
                file_path=Path("/tmp/session.py"),
                agent_id="parent",
                session_generation=2,
                session_id="other-session",
            ),
        )
        for index, route in enumerate(routes):
            _publish_batch(
                mgr,
                DiagnosticBlock(
                    file_path=str(route.file_path),
                    items=[Diagnostic(line=1, character=1, message=str(index))],
                ),
                batch_id=f"batch-{index}",
                route=route,
            )
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
            agent_id="parent",
            session_generation=2,
            session_id="session",
            turn_id="turn",
        )

        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(context)

        assert "[LSP DIAGNOSTICS]" not in context.messages[0]["content"]
        assert len(mgr.pending_diagnostic_batches()) == 3

    def test_injection_error_does_not_ack_and_retry_acks_once(
        self, monkeypatch
    ) -> None:
        from reuleauxcoder.domain.hooks.builtin import lsp_injector
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="/tmp/test.py",
                items=[Diagnostic(line=1, character=1, message="retry")],
            ),
            batch_id="batch-retry",
        )
        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        original = lsp_injector.inject_runtime_overlay_region

        def fail(_messages, _injection):
            raise RuntimeError("inject failed")

        monkeypatch.setattr(lsp_injector, "inject_runtime_overlay_region", fail)
        with pytest.raises(RuntimeError, match="inject failed"):
            hook.run(
                BeforeLLMRequestContext(
                    hook_point=HookPoint.BEFORE_LLM_REQUEST,
                    messages=[_execution_state_tail()],
                )
            )

        assert len(mgr.pending_diagnostic_batches()) == 1
        assert mgr.diagnostic_batch_acknowledgement("batch-retry") is None

        monkeypatch.setattr(lsp_injector, "inject_runtime_overlay_region", original)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )
        hook.run(context)

        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-retry"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-retry") is None

        assert context._commit_dispatch_callbacks() == ()
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-retry") == (
            "lsp-inject:unknown:unknown:unknown"
        )
        assert mgr.diagnostic_batch_metrics()["carried_forward"] == 0
        assert context._commit_dispatch_callbacks() == ()

    def test_failed_overlay_write_does_not_ack(self, monkeypatch) -> None:
        from reuleauxcoder.domain.hooks.builtin import lsp_injector
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="/tmp/test.py",
                items=[Diagnostic(line=1, character=1, message="retry")],
            ),
            batch_id="batch-false",
        )
        monkeypatch.setattr(
            lsp_injector,
            "inject_runtime_overlay_region",
            lambda _messages, _injection: False,
        )

        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(
            BeforeLLMRequestContext(
                hook_point=HookPoint.BEFORE_LLM_REQUEST,
                messages=[_execution_state_tail()],
            )
        )

        assert len(mgr.pending_diagnostic_batches()) == 1
        assert mgr.diagnostic_batch_acknowledgement("batch-false") is None

    def test_injector_ui_failure_is_isolated_after_dispatched_batch_is_acked(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        ui_bus = MagicMock()
        ui_bus.info.side_effect = RuntimeError("SENTINEL_UI_SECRET")
        mgr = _make_manager()
        mgr.ui_bus = ui_bus
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="/tmp/test.py",
                items=[Diagnostic(line=1, character=1, message="retry")],
            ),
            batch_id="batch-ui",
        )

        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )
        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(context)

        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-ui"
        ]
        assert mgr.diagnostic_batch_acknowledgement("batch-ui") is None
        ui_bus.info.assert_not_called()

        failures = context._commit_dispatch_callbacks()

        assert failures == ()
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-ui") == (
            "lsp-inject:unknown:unknown:unknown"
        )
        ui_bus.info.assert_called_once()
        assert "error_type=RuntimeError" in caplog.text
        assert "SENTINEL_UI_SECRET" not in caplog.text

    def test_ack_race_ui_warning_failure_is_isolated_and_secret_safe(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        ui_bus = MagicMock()
        ui_bus.warning.side_effect = RuntimeError("SENTINEL_WARNING_SECRET")
        mgr = _make_manager()
        mgr.ui_bus = ui_bus
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="test.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
            batch_id="batch-race-ui",
        )
        mgr.acknowledge_diagnostic_batches = MagicMock(  # type: ignore[method-assign]
            return_value=False
        )
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )

        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(context)

        assert context._commit_dispatch_callbacks() == ()
        assert "[LSP DIAGNOSTICS]" not in context.messages[0]["content"]
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-race-ui"
        ]
        ui_bus.warning.assert_called_once()
        assert "error_type=RuntimeError" in caplog.text
        assert "SENTINEL_WARNING_SECRET" not in caplog.text

    def test_ack_failure_is_reported_and_diagnostics_are_removed_from_wire(
        self,
    ) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

        mgr = _make_manager()
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="test.py",
                items=[Diagnostic(line=1, character=1, message="err")],
            ),
            batch_id="batch-ack-failure",
        )
        mgr.acknowledge_diagnostic_batches = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("ack failed")
        )
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )

        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(context)
        failures = context._commit_dispatch_callbacks()

        assert len(failures) == 1
        assert str(failures[0]) == "ack failed"
        assert "[LSP DIAGNOSTICS]" not in context.messages[0]["content"]
        assert [batch.batch_id for batch in mgr.pending_diagnostic_batches()] == [
            "batch-ack-failure"
        ]

    def test_filtered_warning_is_terminally_acknowledged(self) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            SEVERITY_WARNING,
        )

        mgr = LspManager(
            LspConfig(enabled=True, include_warnings=False),
            workspace_cwd=Path("/tmp"),
        )
        _publish_batch(
            mgr,
            DiagnosticBlock(
                file_path="/tmp/test.py",
                items=[
                    Diagnostic(
                        line=1,
                        character=1,
                        message="warning",
                        severity=SEVERITY_WARNING,
                    )
                ],
            ),
            batch_id="batch-filtered",
        )
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[_execution_state_tail()],
        )

        LspDiagnosticsInjectorHook(lsp_manager=mgr).run(context)

        assert "[LSP DIAGNOSTICS]" not in context.messages[0]["content"]
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-filtered").startswith(
            "lsp-filtered:"
        )
