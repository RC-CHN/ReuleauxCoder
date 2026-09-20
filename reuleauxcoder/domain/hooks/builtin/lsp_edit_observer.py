"""Prefer immediate edit diagnostics, leaving late results for request injection."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from reuleauxcoder.domain.agent.tool_outcome import ToolDiagnostic
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext
from reuleauxcoder.extensions.lsp.diagnostic_projection import (
    project_diagnostic_results,
)
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager


EDIT_TOOLS = frozenset({"edit_file", "write_file"})


@dataclass(slots=True)
class LspEditObserverHook(TransformHook[AfterToolExecuteContext]):
    """Give the current edit a bounded chance to return its own diagnostics."""

    lsp_manager: LspManager | None = field(default=None)

    def __init__(self, *, lsp_manager: LspManager | None = None, priority: int = 200):
        TransformHook.__init__(
            self, name="lsp_edit_observer", priority=priority, extension_name="core"
        )
        self.lsp_manager = lsp_manager

    @classmethod
    def create_from_config(cls, config: Config) -> LspEditObserverHook:
        return cls()

    def bind_runtime_service(self, name: str, service: object | None) -> None:
        if name == "lsp_manager":
            self.lsp_manager = service  # type: ignore[assignment]

    def clone_for_scope(self, scope: str) -> LspEditObserverHook:
        manager = None if scope == "subagent" else self.lsp_manager
        return LspEditObserverHook(lsp_manager=manager, priority=self.priority)

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        manager = self.lsp_manager
        tool_call = context.tool_call
        if (
            manager is None
            or not manager.enabled
            or tool_call is None
            or tool_call.name not in EDIT_TOOLS
            or context.outcome is None
            or not context.outcome.success
        ):
            return context

        resolved_path = context.outcome.metadata.get("resolved_path")
        file_path = (
            resolved_path
            if isinstance(resolved_path, str) and resolved_path
            else tool_call.arguments.get("file_path")
        )
        if file_path is None:
            return context

        path = Path(file_path)
        batch_id = manager.enqueue_diagnostics(
            path,
            route=DiagnosticRoute(
                file_path=path,
                agent_id=context.agent_id,
                session_generation=context.session_generation,
                session_id=context.session_id,
                turn_id=context.turn_id,
                tool_call_id=tool_call.id,
            ),
            document_committed=True,
        )
        if batch_id is None:
            return context
        result = manager.wait_for_diagnostic_request(
            batch_id,
            timeout=manager.config.edit_wait_timeout_ms / 1000,
            cancellation=context.cancellation,
        )
        if result is None:
            return context
        batches = manager.pending_diagnostic_batches(batch_id=batch_id)
        projection = project_diagnostic_results(
            batches, () if result.is_published else (result,), manager.config
        )
        outcome = context.outcome
        if projection.text is not None:
            outcome = replace(
                outcome,
                model_content=f"{outcome.model_text}\n\n{projection.text}",
                diagnostics=outcome.diagnostics
                + tuple(
                    ToolDiagnostic(
                        path=batch.block.file_path,
                        line=item.line,
                        character=item.character,
                        message=item.message,
                        severity=item.severity_label.lower(),
                        code=item.code,
                    )
                    for batch in batches
                    for item in batch.block.items
                ),
            )
        # Build the complete projection before claiming it. A competing request
        # may have consumed or superseded it while we were rendering.
        if manager.acknowledge_diagnostic_batch(
            batch_id, consumer_id=f"lsp-edit:{tool_call.id}"
        ):
            context.outcome = outcome
            context.result = outcome.model_text
        return context
