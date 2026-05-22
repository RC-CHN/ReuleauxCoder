"""Tests for LSP hook integration.

Tests the LspEditObserverHook (AFTER_TOOL_EXECUTE) and
LspDiagnosticsInjectorHook (BEFORE_LLM_REQUEST) with mocked LspManager.
"""

from __future__ import annotations

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
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.manager import LspManager


def _make_manager() -> LspManager:
    """Create an LspManager with all languages marked unavailable."""
    config = LspConfig(enabled=True)
    mgr = LspManager(config, workspace_cwd=Path("/tmp"))
    for lang in range(10):  # all LanguageId values
        mgr._availability[lang] = False
    return mgr


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
        )
        hook.run(context)
        assert len(mgr._notification_queue) == 1
        kind, path = mgr._notification_queue[0]
        assert kind == "did_save"

    def test_all_edit_tools_are_handled(self) -> None:
        """Verify that the EDIT_TOOLS set covers all expected edit tools."""
        for tool_name in EDIT_TOOLS:
            assert _extract_file_path(tool_name, {"file_path": "f.py"}) == "f.py"


class TestLspEditObserverCreateFromConfig:
    def test_create_from_config(self) -> None:
        hook = LspEditObserverHook.create_from_config(MagicMock())
        assert hook.lsp_manager is None
        assert hook.name == "lsp_edit_observer"

    def test_set_lsp_manager(self) -> None:
        hook = LspEditObserverHook.create_from_config(MagicMock())
        mgr = _make_manager()
        hook.set_lsp_manager(mgr)
        assert hook.lsp_manager is mgr


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
        with mgr._lock:
            mgr._results[Path("/tmp/test.py")] = block

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
        with mgr._lock:
            mgr._results[Path("/tmp/test.py")] = block

        assert len(mgr.drain_diagnostics()) == 1  # confirm data is there
        # Re-add for the hook test
        with mgr._lock:
            mgr._results[Path("/tmp/test.py")] = block

        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[],
        )
        hook.run(context)
        # Results should be drained after hook runs
        assert len(mgr.drain_diagnostics()) == 0


class TestLspDiagnosticsInjectorCreateFromConfig:
    def test_create_from_config(self) -> None:
        hook = LspDiagnosticsInjectorHook.create_from_config(MagicMock())
        assert hook.lsp_manager is None
        assert hook.name == "lsp_diagnostics_injector"

    def test_set_lsp_manager(self) -> None:
        hook = LspDiagnosticsInjectorHook.create_from_config(MagicMock())
        mgr = _make_manager()
        hook.set_lsp_manager(mgr)
        assert hook.lsp_manager is mgr


# === LspManager dedup flag ===


class TestLspManagerDedupFlag:
    def test_flag_starts_false(self) -> None:
        mgr = _make_manager()
        assert mgr._diagnostics_fed is False

    def test_mark_diagnostics_fed_sets_flag(self) -> None:
        mgr = _make_manager()
        mgr.mark_diagnostics_fed()
        assert mgr._diagnostics_fed is True

    def test_consume_returns_and_resets(self) -> None:
        mgr = _make_manager()
        mgr.mark_diagnostics_fed()
        assert mgr.consume_diagnostics_fed_flag() is True
        # Flag is reset after consumption
        assert mgr.consume_diagnostics_fed_flag() is False

    def test_consume_false_when_not_marked(self) -> None:
        mgr = _make_manager()
        assert mgr.consume_diagnostics_fed_flag() is False


# === LspEditObserverHook dedup ===


class TestLspEditObserverDedup:
    def test_marks_fed_after_injecting_diagnostics(self) -> None:
        """When the edit observer drains and injects diagnostics, it
        should call mark_diagnostics_fed() so the injector skips."""
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            DiagnosticBlock,
        )
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        mgr = _make_manager()
        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        # Pre-populate results so drain_diagnostics returns a block
        block = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        with mgr._lock:
            mgr._results[Path("/tmp/test.py")] = block

        assert mgr._diagnostics_fed is False

        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            round_index=1,
        )
        hook.run(context)

        # After injecting: flag is True
        assert mgr._diagnostics_fed is True
        # Tool result should contain the diagnostics
        assert context.result is not None
        assert "err" in context.result

    def test_does_not_mark_when_diagnostics_empty(self) -> None:
        """When drain returns no blocks, mark_diagnostics_fed is NOT called."""
        from reuleauxcoder.extensions.lsp.registry import LanguageId

        mgr = _make_manager()
        with mgr._lock:
            mgr._availability[LanguageId.PYTHON] = True

        # Results dict is empty — drain will return []
        assert mgr._diagnostics_fed is False

        hook = LspEditObserverHook(lsp_manager=mgr)
        context = AfterToolExecuteContext(
            hook_point=HookPoint.AFTER_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="1",
                name="edit_file",
                arguments={"file_path": "/tmp/test.py"},
            ),
            round_index=1,
        )
        hook.run(context)

        # No diagnostics injected → flag stays False
        assert mgr._diagnostics_fed is False


# === LspDiagnosticsInjectorHook dedup ===


class TestLspDiagnosticsInjectorDedup:
    def test_skips_when_flag_set(self) -> None:
        """When consume_diagnostics_fed_flag() returns True, the injector
        drains the queue but skips injecting into messages."""
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            DiagnosticBlock,
        )

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        with mgr._lock:
            mgr._results[Path("/tmp/test.py")] = block
            mgr._diagnostics_fed = True  # simulate edit observer

        hook = LspDiagnosticsInjectorHook(lsp_manager=mgr)
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[{"role": "user", "content": "hello</system_context>"}],
        )
        result = hook.run(context)

        # Content unchanged — no injection
        assert "[LSP DIAGNOSTICS]" not in result.messages[0]["content"]
        assert result.messages[0]["content"] == "hello</system_context>"
        # Queue is drained anyway
        assert len(mgr.drain_diagnostics()) == 0
        # Flag is consumed/reset
        assert mgr._diagnostics_fed is False

    def test_still_injects_when_flag_not_set(self) -> None:
        """Normal path: flag is False → injector proceeds as usual."""
        from reuleauxcoder.extensions.lsp.diagnostics import (
            Diagnostic,
            DiagnosticBlock,
        )

        mgr = _make_manager()
        block = DiagnosticBlock(
            file_path="/tmp/test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        with mgr._lock:
            mgr._results[Path("/tmp/test.py")] = block
            # flag is False (default)

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
        assert len(mgr.drain_diagnostics()) == 0
        # Flag was consumed (was False, stays False)
        assert mgr._diagnostics_fed is False
