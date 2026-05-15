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
            messages=[{"role": "user", "content": "hello"}],
        )
        result = hook.run(context)

        # Should have 2 messages: injected diagnostic + original
        assert len(result.messages) == 2
        # First message should be the diagnostics
        assert result.messages[0]["role"] == "user"
        assert "err" in result.messages[0]["content"]
        # Second should be original
        assert result.messages[1]["content"] == "hello"

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
