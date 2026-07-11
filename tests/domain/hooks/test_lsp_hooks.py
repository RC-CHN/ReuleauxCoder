"""Tests for LSP hook integration.

Tests the LspEditObserverHook (AFTER_TOOL_EXECUTE) and
LspDiagnosticsInjectorHook (BEFORE_LLM_REQUEST) with mocked LspManager.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

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

    def enqueue(file_path: Path, *, route=None):
        assert route is not None
        batch_id = f"batch-{route.tool_call_id}"
        _publish_batch(mgr, block, batch_id=batch_id, route=route)
        return batch_id

    mgr.enqueue_diagnostics = MagicMock(side_effect=enqueue)  # type: ignore[method-assign]


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

    def test_enqueues_notification_for_edit_tools(self) -> None:
        mgr = _make_manager()
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        hook = LspEditObserverHook(lsp_manager=mgr)
        assert len(mgr._notification_queue) == 0
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
        assert len(mgr._notification_queue) == 1
        kind, path = mgr._notification_queue[0]
        assert kind == "did_save"

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

        assert len(mgr._notification_queue) == 0
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
            messages=[{"role": "user", "content": "hello</system_context>"}],
        )
        result = hook.run(context)

        # Diagnostics are appended inside the <system_context> tail of the last
        # user message, not prepended as a separate message.
        assert len(result.messages) == 1
        assert "err" in result.messages[0]["content"]
        assert "hello" in result.messages[0]["content"]
        assert "[LSP DIAGNOSTICS]" in result.messages[0]["content"]

    def test_drains_and_clears_diagnostics(self) -> None:
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
        # Results should be drained after hook runs
        assert mgr.pending_diagnostic_batches() == ()
        assert mgr.diagnostic_batch_acknowledgement("batch-1") is not None


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
        assert [item.message for item in tool_context.outcome.diagnostics] == [
            "edited"
        ]
        remaining = mgr.pending_diagnostic_batches()
        assert [batch.block.file_path for batch in remaining] == ["/tmp/other.py"]


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
            messages=[{"role": "user", "content": "hello</system_context>"}],
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
            messages=[{"role": "user", "content": "hello</system_context>"}],
        )
        result = hook.run(context)

        # Injection happened
        assert "[LSP DIAGNOSTICS]" in result.messages[0]["content"]
        assert "err" in result.messages[0]["content"]
        # Queue drained
        assert mgr.pending_diagnostic_batches() == ()
