from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import LspEditObserverHook
from reuleauxcoder.domain.hooks.builtin.lsp_injector import (
    LspDiagnosticsInjectorHook,
)
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.lsp.client import LspFailureFacts
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBatch,
    DiagnosticBlock,
)
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _manager() -> LspManager:
    manager = LspManager(LspConfig(enabled=True), workspace_cwd=Path("/tmp"))
    manager.start_worker = MagicMock()  # type: ignore[method-assign]
    manager._accepting_work = True
    manager._availability[LanguageId.PYTHON] = True
    return manager


def _failure(
    manager: LspManager,
    *,
    batch_id: str,
    route: DiagnosticRoute,
    status: DiagnosticOutcomeStatus = DiagnosticOutcomeStatus.ERROR,
) -> DiagnosticOutcome:
    outcome = DiagnosticOutcome(
        batch_id=batch_id,
        route=route,
        request_sequence=1,
        status=status,
        created_at=manager._diagnostic_clock(),
        failure=(
            None
            if status is DiagnosticOutcomeStatus.STALE_DISCARDED
            else LspFailureFacts(
                phase="document_sync",
                error_type="LspDocumentReadError",
                language="python",
                root_hash="abc123",
            )
        ),
    )
    manager._diagnostic_failure_outcomes[batch_id] = outcome
    return outcome


def _execution_state_tail() -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            '<execution_state plan_revision="0">\n'
            '<execution_data trust="untrusted_data">\n{}\n</execution_data>\n'
            "<runtime_instruction>Continue.</runtime_instruction>\n"
            "</execution_state>"
        ),
    }


def test_edit_observer_returns_real_diagnostics_failure_to_agent() -> None:
    manager = _manager()
    route = DiagnosticRoute(
        file_path=Path("/tmp/main.py"),
        agent_id="agent",
        session_generation=1,
        session_id="session",
        turn_id="turn",
        tool_call_id="edit-1",
    )
    outcome = _failure(manager, batch_id="failed", route=route)
    manager.enqueue_diagnostics = MagicMock(  # type: ignore[method-assign]
        return_value=outcome.batch_id
    )
    context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="edit-1",
            name="edit_file",
            arguments={"file_path": "/tmp/main.py"},
        ),
        outcome=ToolOutcome(content="edited"),
        agent_id="agent",
        session_generation=1,
        session_id="session",
        turn_id="turn",
    )

    LspEditObserverHook(lsp_manager=manager).run(context)

    assert context.outcome is not None
    assert context.outcome.success
    assert "edited" in context.outcome.model_text
    assert "[LSP DIAGNOSTICS OUTCOME]" in context.outcome.model_text
    assert "status=error" in context.outcome.model_text
    assert "error_type=LspDocumentReadError" in context.outcome.model_text
    assert manager.diagnostic_request_outcome(outcome.batch_id) is None
    assert manager.diagnostic_batch_acknowledgement(outcome.batch_id) == (
        "lsp-edit:edit-1"
    )


def test_late_failure_is_injected_and_acknowledged_only_after_dispatch() -> None:
    manager = _manager()
    route = DiagnosticRoute(
        file_path=Path("/tmp/main.py"),
        agent_id="agent",
        session_generation=1,
        session_id="session",
        turn_id="origin",
        tool_call_id="edit-1",
    )
    outcome = _failure(manager, batch_id="late-failure", route=route)
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[_execution_state_tail()],
        agent_id="agent",
        session_generation=1,
        session_id="session",
        turn_id="next",
    )

    LspDiagnosticsInjectorHook(lsp_manager=manager).run(context)

    payload = context.messages[0]["content"]
    assert "<lsp_diagnostic_outcomes>" in payload
    assert "status=error" in payload
    assert "error_type=LspDocumentReadError" in payload
    assert manager.diagnostic_request_outcome(outcome.batch_id) is outcome

    assert context._commit_dispatch_callbacks() == ()
    assert manager.diagnostic_request_outcome(outcome.batch_id) is None
    assert manager.diagnostic_batch_acknowledgement(outcome.batch_id) == (
        "lsp-inject:agent:1:next"
    )
    assert manager.diagnostic_batch_metrics()["carried_forward"] == 1


def test_edit_observer_ack_fault_does_not_crash_or_claim_delivery() -> None:
    manager = _manager()
    route = DiagnosticRoute(file_path=Path("/tmp/main.py"), tool_call_id="edit-1")
    outcome = _failure(manager, batch_id="retry-ack", route=route)
    manager.enqueue_diagnostics = MagicMock(  # type: ignore[method-assign]
        return_value=outcome.batch_id
    )
    acknowledge = manager.acknowledge_diagnostic_batch
    manager.acknowledge_diagnostic_batch = MagicMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("secondary observer failure")
    )
    context = AfterToolExecuteContext(
        hook_point=HookPoint.AFTER_TOOL_EXECUTE,
        tool_call=ToolCall(
            id="edit-1",
            name="edit_file",
            arguments={"file_path": "/tmp/main.py"},
        ),
        outcome=ToolOutcome(content="edited"),
    )

    LspEditObserverHook(lsp_manager=manager).run(context)

    assert context.outcome is not None
    assert context.outcome.model_text == "edited"
    manager.acknowledge_diagnostic_batch = acknowledge  # type: ignore[method-assign]
    assert manager.diagnostic_request_outcome(outcome.batch_id) is outcome
    assert manager.diagnostic_batch_acknowledgement(outcome.batch_id) is None


def test_failed_overlay_projection_keeps_failure_retryable(
    monkeypatch,
) -> None:
    from reuleauxcoder.domain.hooks.builtin import lsp_injector

    manager = _manager()
    route = DiagnosticRoute(file_path=Path("/tmp/main.py"))
    outcome = _failure(manager, batch_id="retry", route=route)
    monkeypatch.setattr(
        lsp_injector,
        "inject_runtime_overlay_region",
        lambda _messages, _injection: False,
    )
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[_execution_state_tail()],
    )

    LspDiagnosticsInjectorHook(lsp_manager=manager).run(context)

    assert manager.diagnostic_request_outcome(outcome.batch_id) is outcome
    assert manager.diagnostic_batch_acknowledgement(outcome.batch_id) is None


def test_injector_atomically_acknowledges_publish_and_failure_together() -> None:
    manager = _manager()
    route = DiagnosticRoute(file_path=Path("/tmp/main.py"))
    failure = _failure(manager, batch_id="failure", route=route)
    batch = DiagnosticBatch(
        batch_id="published",
        route=DiagnosticRoute(file_path=Path("/tmp/other.py")),
        request_sequence=2,
        document_version=1,
        diagnostic_generation=1,
        block=DiagnosticBlock(
            file_path="other.py",
            items=[Diagnostic(line=1, character=1, message="broken")],
        ),
    )
    manager._diagnostic_batches[batch.batch_id] = batch
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[_execution_state_tail()],
    )

    LspDiagnosticsInjectorHook(lsp_manager=manager).run(context)

    assert "broken" in context.messages[0]["content"]
    assert "status=error" in context.messages[0]["content"]
    assert manager.diagnostic_request_outcome(failure.batch_id) is failure
    assert manager.pending_diagnostic_batches() == (batch,)

    assert context._commit_dispatch_callbacks() == ()
    assert manager.diagnostic_request_outcome(failure.batch_id) is None
    assert manager.pending_diagnostic_batches() == ()


def test_stale_terminal_is_visible_without_fake_clean_diagnostics() -> None:
    manager = _manager()
    route = DiagnosticRoute(file_path=Path("/tmp/main.py"))
    outcome = _failure(
        manager,
        batch_id="stale",
        route=route,
        status=DiagnosticOutcomeStatus.STALE_DISCARDED,
    )
    context = BeforeLLMRequestContext(
        hook_point=HookPoint.BEFORE_LLM_REQUEST,
        messages=[_execution_state_tail()],
    )

    LspDiagnosticsInjectorHook(lsp_manager=manager).run(context)

    payload = context.messages[0]["content"]
    assert "status=stale_discarded" in payload
    assert "PUBLISHED_CLEAN" not in payload
    assert manager.diagnostic_request_outcome(outcome.batch_id) is outcome
