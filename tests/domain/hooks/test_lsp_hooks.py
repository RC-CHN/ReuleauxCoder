"""Tests for LSP hook integration.

Tests the LspEditObserverHook (AFTER_TOOL_EXECUTE) and
LspDiagnosticsInjectorHook (BEFORE_LLM_REQUEST) with mocked LspManager.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import (
    LspEditObserverHook,
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


@pytest.mark.parametrize("tool_name", ["edit_file", "write_file"])
def test_edits_queue_document_commit_without_waiting(tool_name: str) -> None:
    from reuleauxcoder.extensions.lsp.registry import LanguageId

    manager = _make_manager()
    manager._availability[LanguageId.PYTHON] = True
    original = ToolOutcome(
        content="edited",
        metadata={"resolved_path": "/tmp/canonical.py"},
        model_content="edited with refreshed approval",
    )
    context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        agent_id="agent",
        session_generation=1,
        session_id="session",
        turn_id="turn",
        tool_call=ToolCall(
            id="edit", name=tool_name, arguments={"file_path": "relative.py"}
        ),
        outcome=original,
    )
    LspEditObserverHook(lsp_manager=manager).run(context)

    assert context.outcome is original
    assert len(manager._diagnostics_queue) == 1
    request = manager._diagnostics_queue[0]
    assert request.route == DiagnosticRoute(
        file_path=Path("/tmp/canonical.py").resolve(),
        agent_id="agent",
        session_generation=1,
        session_id="session",
        turn_id="turn",
        tool_call_id="edit",
    )
    assert request.document_committed
    assert request.batch_id in manager._pending_diagnostic_requests


def test_failed_edit_does_not_enqueue_diagnostics() -> None:
    manager = _make_manager()
    context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="edit", name="edit_file", arguments={"file_path": "/tmp/test.py"}
        ),
        outcome=ToolOutcome(
            status=ToolOutcomeStatus.FAILED,
            content="edit failed",
            error_kind=ToolErrorKind.EXECUTION,
        ),
    )
    LspEditObserverHook(lsp_manager=manager).run(context)
    assert manager._diagnostics_queue == []


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


@pytest.mark.parametrize("model_content", [None, "edited with refreshed approval"])
def test_ready_edit_diagnostics_have_one_delivery_path(
    model_content: str | None,
) -> None:
    from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic

    manager = _make_manager()
    block = DiagnosticBlock("/tmp/test.py", [Diagnostic(1, 1, "actual error")])
    _complete_enqueued_batch(manager, block)
    original = ToolOutcome(content="edited", model_content=model_content)
    edit_context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="1", name="edit_file", arguments={"file_path": "/tmp/test.py"}
        ),
        outcome=original,
    )
    LspEditObserverHook(lsp_manager=manager).run(edit_context)
    assert edit_context.outcome is original
    assert manager.diagnostic_batch_acknowledgement("batch-1") is None

    messages = [
        {"role": "tool", "content": original.model_text},
        _execution_state_tail(),
    ]
    request = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST, messages=messages
    )
    injector = LspDiagnosticsInjectorHook(lsp_manager=manager)
    injector.run(request)
    assert request.messages[0]["content"] == original.model_text
    assert "actual error" in request.messages[-1]["content"]
    assert manager.diagnostic_batch_acknowledgement("batch-1") is None
    assert request._commit_dispatch_callbacks() == ()
    assert manager.pending_diagnostic_batches() == ()

    next_request = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST, messages=[_execution_state_tail()]
    )
    injector.run(next_request)
    assert "actual error" not in next_request.messages[0]["content"]


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
            SEVERITY_WARNING,
            Diagnostic,
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
