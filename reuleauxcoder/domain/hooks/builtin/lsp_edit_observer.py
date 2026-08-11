"""LSP edit outcome processor — triggers diagnostics after file edits.

AFTER_TOOL_EXECUTE transform:
- Detects edit_file / write_file tool calls
- Extracts edited file paths
- Enqueues one ordered document-commit diagnostics request
- The worker synchronizes content, sends didSave, then observes diagnostics
- Polls briefly for diagnostics and appends them to the tool result so the
  model sees any errors immediately (no one-turn delay).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.agent.tool_outcome import ToolDiagnostic
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    render_diagnostic_outcomes,
    safe_observer_error_type,
)
from reuleauxcoder.interfaces.events import UIEventKind

EDIT_TOOLS = frozenset({"edit_file", "write_file"})
_DIAGNOSTICS_POLL_DEADLINE = 2.5  # seconds — short poll for instant feedback
_DIAGNOSTICS_POLL_INTERVAL = 0.1

logger = logging.getLogger(__name__)


def _extract_file_path(tool_name: str, arguments: dict) -> str | None:
    """Extract the file path from a tool call's arguments.

    Handles edit_file and write_file — both use 'file_path' as the key.
    """
    return arguments.get("file_path")


@dataclass(slots=True)
class LspEditObserverHook(TransformHook[AfterToolExecuteContext]):
    """Trigger LSP diagnostics and didSave after file edits."""

    lsp_manager: LspManager | None = field(default=None)

    def __init__(
        self,
        *,
        lsp_manager: LspManager | None = None,
        priority: int = 200,
    ):
        TransformHook.__init__(
            self,
            name="lsp_edit_observer",
            priority=priority,
            extension_name="core",
        )
        self.lsp_manager = lsp_manager

    @classmethod
    def create_from_config(cls, config: "Config") -> "LspEditObserverHook":
        """Create hook instance from config.  LspManager injected later."""
        return cls(lsp_manager=None, priority=200)

    def bind_runtime_service(self, name: str, service: object | None) -> None:
        if name == "lsp_manager":
            self.lsp_manager = service  # type: ignore[assignment]

    def clone_for_scope(self, scope: str) -> "LspEditObserverHook":
        # A subagent has no independent workspace LSP scope yet. Disabling the
        # observer is safer than sharing/draining the parent's manager.
        manager = None if scope == "subagent" else self.lsp_manager
        return LspEditObserverHook(lsp_manager=manager, priority=self.priority)

    def run(self, context: AfterToolExecuteContext) -> AfterToolExecuteContext:
        """Detect edit tools, enqueue diagnostics, and try to inject them
        immediately into the tool result.
        """
        if self.lsp_manager is None:
            return context

        if not self.lsp_manager.enabled:
            return context

        tool_call = context.tool_call
        if tool_call is None:
            return context

        if tool_call.name not in EDIT_TOOLS:
            return context

        # edit/write now report explicit status.  Never notify LSP about a
        # failed mutation or diagnose a file view that was not actually saved.
        if context.outcome is None or not context.outcome.success:
            return context

        resolved_path = context.outcome.metadata.get("resolved_path")
        file_path = (
            resolved_path
            if isinstance(resolved_path, str) and resolved_path
            else _extract_file_path(tool_call.name, tool_call.arguments)
        )
        if file_path is None:
            return context

        path = Path(file_path)

        # Enqueue one ordered commit request. The LSP worker synchronizes the
        # document, sends didSave, and only then observes diagnostics.
        route = DiagnosticRoute(
            file_path=path,
            agent_id=context.agent_id,
            session_generation=context.session_generation,
            session_id=context.session_id,
            turn_id=context.turn_id,
            tool_call_id=tool_call.id,
        )
        batch_id = self.lsp_manager.enqueue_diagnostics(
            path,
            route=route,
            document_committed=True,
        )
        if batch_id is None:
            return context

        # Short synchronous poll — if the worker has already produced
        #    diagnostics, append them directly to the tool result so the
        #    model sees them immediately.
        deadline = time.monotonic() + _DIAGNOSTICS_POLL_DEADLINE
        batches = ()
        terminal_outcome: DiagnosticOutcome | None = None
        while time.monotonic() < deadline:
            result = self.lsp_manager.diagnostic_request_result(batch_id)
            if result is not None:
                batches = result
                terminal_outcome = self.lsp_manager.diagnostic_request_outcome(batch_id)
                break
            time.sleep(_DIAGNOSTICS_POLL_INTERVAL)

        if terminal_outcome is not None and not terminal_outcome.is_published:
            rendered_outcome = render_diagnostic_outcomes((terminal_outcome,))
            if rendered_outcome is not None:
                previous_outcome = context.outcome
                previous_result = context.result
                model_text = previous_outcome.model_text
                projected = (
                    f"{model_text}\n\n[LSP DIAGNOSTICS OUTCOME]\n{rendered_outcome}"
                    if model_text
                    else f"[LSP DIAGNOSTICS OUTCOME]\n{rendered_outcome}"
                )
                context.outcome = replace(
                    previous_outcome,
                    model_content=projected,
                    metadata={
                        **dict(previous_outcome.metadata),
                        "lsp_diagnostic_outcome_ids": (terminal_outcome.batch_id,),
                    },
                )
                context.result = context.outcome.model_text
                try:
                    acknowledged = self.lsp_manager.acknowledge_diagnostic_batch(
                        terminal_outcome.batch_id,
                        consumer_id=f"lsp-edit:{tool_call.id}",
                    )
                except Exception as error:
                    # A bookkeeping observer fault must not turn a successful
                    # edit into a fatal tool failure.  Restore the projection
                    # so the retained outcome remains available for retry.
                    context.outcome = previous_outcome
                    context.result = previous_result
                    logger.warning(
                        "LSP diagnostic outcome acknowledgement failed: error_type=%s",
                        safe_observer_error_type(error),
                    )
                else:
                    if not acknowledged:
                        # Another consumer won the exact outcome.  Avoid
                        # claiming duplicate delivery in this tool result.
                        context.outcome = previous_outcome
                        context.result = previous_result

        if batches:
            blocks = [batch.block for batch in batches]
            # Count errors / warnings for UI feedback
            err_count = 0
            warn_count = 0
            for block in blocks:
                for d in block.items:
                    if d.is_error:
                        err_count += 1
                    elif d.is_warning:
                        warn_count += 1

            diagnostics: list[ToolDiagnostic] = []
            for block in blocks:
                items = block.items
                if not self.lsp_manager.config.include_warnings:
                    items = [item for item in items if item.is_error]
                for item in sorted(
                    items[: self.lsp_manager.config.max_diagnostics],
                    key=lambda diagnostic: (diagnostic.severity, diagnostic.line),
                ):
                    diagnostics.append(
                        ToolDiagnostic(
                            path=block.file_path,
                            line=item.line,
                            character=item.character,
                            message=item.message,
                            severity=item.severity_label.lower(),
                            code=item.code,
                            source="lsp",
                        )
                    )
            if diagnostics:
                metadata = {
                    **dict(context.outcome.metadata),
                    "lsp_batch_ids": tuple(batch.batch_id for batch in batches),
                }
                context.outcome = replace(
                    context.outcome,
                    diagnostics=context.outcome.diagnostics + tuple(diagnostics),
                    metadata=metadata,
                )
                context.result = context.outcome.model_text

            # Emit a compact UI feedback panel
            ui_bus = getattr(self.lsp_manager, "ui_bus", None)
            if ui_bus is not None:
                parts: list[str] = []
                if err_count:
                    parts.append(f"{err_count} error{'s' if err_count != 1 else ''}")
                if warn_count:
                    parts.append(
                        f"{warn_count} warning{'s' if warn_count != 1 else ''}"
                    )
                if parts:
                    try:
                        ui_bus.info(
                            f"LSP: {', '.join(parts)} after {tool_call.name}",
                            kind=UIEventKind.SYSTEM,
                        )
                    except Exception as error:
                        # UI feedback is a secondary observer.  Diagnostics
                        # already projected for the agent remain successful and
                        # are acknowledged below; the observer fault is still
                        # recorded without crashing the edit result.
                        logger.warning(
                            "LSP edit diagnostics UI observer failed: error_type=%s",
                            safe_observer_error_type(error),
                        )

            # Claim the exact projected set atomically.  A bookkeeping fault
            # must not crash a successful edit or pretend the batches were
            # consumed; they remain available to the request-time injector.
            batch_ids = tuple(batch.batch_id for batch in batches)
            try:
                self.lsp_manager.acknowledge_diagnostic_batches(
                    batch_ids,
                    consumer_id=f"lsp-edit:{tool_call.id}",
                )
            except Exception as error:
                logger.warning(
                    "LSP edit diagnostics acknowledgement failed: error_type=%s",
                    safe_observer_error_type(error),
                )
        return context
