"""LSP edit observer hook — triggers diagnostics and didSave after file edits.

AFTER_TOOL_EXECUTE observer (fail-open):
- Detects edit_file / write_file tool calls
- Extracts edited file paths
- Enqueues diagnostics request (fire-and-forget)
- Sends didSave notification (fire-and-forget)
- Polls briefly for diagnostics and appends them to the tool result so the
  model sees any errors immediately (no one-turn delay).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.lsp.manager import LspManager

from reuleauxcoder.domain.hooks.base import ObserverHook
from reuleauxcoder.domain.hooks.discovery import register_hook
from reuleauxcoder.domain.hooks.types import AfterToolExecuteContext, HookPoint
from reuleauxcoder.extensions.lsp.diagnostics import render_blocks
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
class LspEditObserverHook(ObserverHook[AfterToolExecuteContext]):
    """Trigger LSP diagnostics and didSave after file edits."""

    lsp_manager: LspManager | None = field(default=None)

    def __init__(
        self,
        *,
        lsp_manager: LspManager | None = None,
        priority: int = 200,
    ):
        ObserverHook.__init__(
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

    def set_lsp_manager(self, mgr: "LspManager") -> None:
        """Inject the LspManager reference post-construction."""
        self.lsp_manager = mgr

    def run(self, context: AfterToolExecuteContext) -> None:
        """Detect edit tools, enqueue diagnostics, and try to inject them
        immediately into the tool result.
        """
        if self.lsp_manager is None:
            return

        if not self.lsp_manager.enabled:
            return

        tool_call = context.tool_call
        if tool_call is None:
            return

        if tool_call.name not in EDIT_TOOLS:
            return

        file_path = _extract_file_path(tool_call.name, tool_call.arguments)
        if file_path is None:
            return

        path = Path(file_path)

        # 1. Notify LSP server that the file was saved
        self.lsp_manager.notify_did_save(path)

        # 2. Enqueue diagnostics request (fire-and-forget)
        self.lsp_manager.enqueue_diagnostics(path, seq=context.round_index or 0)

        # 3. Short synchronous poll — if the worker has already produced
        #    diagnostics, append them directly to the tool result so the
        #    model sees them immediately.
        deadline = time.monotonic() + _DIAGNOSTICS_POLL_DEADLINE
        blocks = []
        while time.monotonic() < deadline:
            blocks = self.lsp_manager.drain_diagnostics()
            if blocks:
                break
            time.sleep(_DIAGNOSTICS_POLL_INTERVAL)

        if blocks:
            # Count errors / warnings for UI feedback
            err_count = 0
            warn_count = 0
            for block in blocks:
                for d in block.items:
                    if d.is_error:
                        err_count += 1
                    elif d.is_warning:
                        warn_count += 1

            rendered = render_blocks(
                blocks,
                max_diagnostics=self.lsp_manager.config.max_diagnostics,
                include_warnings=self.lsp_manager.config.include_warnings,
            )
            if rendered:
                suffix = "\n\n" + rendered
                context.result = (context.result or "") + suffix

            # Mark that diagnostics were already fed to the model so the
            # BEFORE_LLM_REQUEST injector skips this turn.
            self.lsp_manager.mark_diagnostics_fed()

            # Emit a compact UI feedback panel
            ui_bus = getattr(self.lsp_manager, "ui_bus", None)
            if ui_bus is not None:
                parts: list[str] = []
                if err_count:
                    parts.append(f"{err_count} error{'s' if err_count != 1 else ''}")
                if warn_count:
                    parts.append(f"{warn_count} warning{'s' if warn_count != 1 else ''}")
                if parts:
                    ui_bus.info(
                        f"LSP: {', '.join(parts)} after {tool_call.name}",
                        kind=UIEventKind.SYSTEM,
                    )
