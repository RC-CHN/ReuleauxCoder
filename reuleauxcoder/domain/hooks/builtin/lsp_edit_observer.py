"""LSP edit outcome processor — triggers diagnostics after file edits.

AFTER_TOOL_EXECUTE transform:
- Detects edit_file / write_file tool calls
- Extracts edited file paths
- Enqueues diagnostics request (fire-and-forget)
- Sends didSave notification (fire-and-forget)
- Polls briefly for diagnostics and appends them to the tool result so the
  model sees any errors immediately (no one-turn delay).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.agent.tool_outcome import ToolDiagnostic
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.interfaces.events import UIEventKind

EDIT_TOOLS = frozenset({"edit_file", "write_file"})
_DIAGNOSTICS_POLL_DEADLINE = 2.5  # seconds — short poll for instant feedback
_DIAGNOSTICS_POLL_INTERVAL = 0.1


def _extract_file_path(tool_name: str, arguments: dict) -> str | None:
    """Extract the file path from a tool call's arguments.

    Handles edit_file and write_file — both use 'file_path' as the key.
    """
    return arguments.get("file_path")


@register_hook(HookPoint.AFTER_TOOL_EXECUTE, priority=200)
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

        file_path = _extract_file_path(tool_call.name, tool_call.arguments)
        if file_path is None:
            return context

        path = Path(file_path)

        # 1. Notify LSP server that the file was saved
        self.lsp_manager.notify_did_save(path)

        # 2. Enqueue diagnostics request (fire-and-forget)
        route = DiagnosticRoute(
            file_path=path,
            agent_id=context.agent_id,
            session_generation=context.session_generation,
            session_id=context.session_id,
            turn_id=context.turn_id,
            tool_call_id=tool_call.id,
        )
        batch_id = self.lsp_manager.enqueue_diagnostics(path, route=route)
        if batch_id is None:
            return context

        # 3. Short synchronous poll — if the worker has already produced
        #    diagnostics, append them directly to the tool result so the
        #    model sees them immediately.
        deadline = time.monotonic() + _DIAGNOSTICS_POLL_DEADLINE
        batches = ()
        while time.monotonic() < deadline:
            batches = self.lsp_manager.consume_diagnostic_batches(
                consumer_id=f"lsp-edit:{tool_call.id}",
                batch_id=batch_id,
            )
            if batches:
                break
            time.sleep(_DIAGNOSTICS_POLL_INTERVAL)

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
                    ui_bus.info(
                        f"LSP: {', '.join(parts)} after {tool_call.name}",
                        kind=UIEventKind.SYSTEM,
                    )
        return context
