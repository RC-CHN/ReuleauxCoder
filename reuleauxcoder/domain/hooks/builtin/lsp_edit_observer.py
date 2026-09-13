"""Queue diagnostics after successful edits; request-time injection owns delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager


EDIT_TOOLS = frozenset({"edit_file", "write_file"})


@dataclass(slots=True)
class LspEditObserverHook(TransformHook[AfterToolExecuteContext]):
    """Synchronize committed files asynchronously without delaying tool results."""

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
        manager.enqueue_diagnostics(
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
        return context
