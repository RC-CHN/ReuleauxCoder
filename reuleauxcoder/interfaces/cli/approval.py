"""CLI approval interaction provider."""

from __future__ import annotations

import difflib
import threading
from pathlib import Path

from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalProvider, ApprovalRequest
from reuleauxcoder.interfaces.cli.render import render_diff_panel
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


class CLIApprovalProvider(ApprovalProvider):
    """Minimal interactive approval provider for CLI mode."""

    def __init__(self, ui_bus: UIEventBus):
        self.ui_bus = ui_bus
        self._approval_lock = threading.Lock()

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        # Approval can be requested from parallel tool threads; serialize terminal I/O.
        with self._approval_lock:
            self.ui_bus.warning(
                f"Approval required for tool '{request.tool_name}' ({request.tool_source})",
                kind=UIEventKind.COMMAND,
            )
            if request.reason:
                self.ui_bus.info(f"Reason: {request.reason}", kind=UIEventKind.COMMAND)

            diff_text = self._build_preview_diff(request)
            if diff_text is not None:
                title = "Proposed file diff:" if request.tool_name == "write_file" else "Proposed edit diff:"
                self.ui_bus.info(title, kind=UIEventKind.COMMAND)
                render_diff_panel(diff_text)
            elif request.tool_args:
                self.ui_bus.info(
                    f"Args: {request.tool_args}",
                    kind=UIEventKind.COMMAND,
                )

            while True:
                answer = input("Approve tool execution? [y/n]: ").strip().lower()
                if answer in {"y", "yes"}:
                    return ApprovalDecision.allow_once("approved in CLI")
                if answer in {"n", "no"}:
                    return ApprovalDecision.deny_once("denied in CLI")
                self.ui_bus.warning("Please enter 'y' or 'n'.", kind=UIEventKind.COMMAND)

    def _build_preview_diff(self, request: ApprovalRequest) -> str | None:
        """Build a readable preview diff for file-changing approvals when possible."""
        file_path = request.tool_args.get("file_path")
        if not isinstance(file_path, str):
            return None

        if request.tool_name == "edit_file":
            old_string = request.tool_args.get("old_string")
            new_string = request.tool_args.get("new_string")
            if not isinstance(old_string, str) or not isinstance(new_string, str):
                return None

            try:
                content = Path(file_path).expanduser().resolve().read_text()
            except Exception:
                return None

            if content.count(old_string) != 1:
                return None

            return self._unified_diff(
                content,
                content.replace(old_string, new_string, 1),
                file_path,
            )

        if request.tool_name == "write_file":
            new_content = request.tool_args.get("content")
            if not isinstance(new_content, str):
                return None

            path = Path(file_path).expanduser().resolve()
            try:
                old_content = path.read_text() if path.exists() else ""
            except Exception:
                old_content = ""

            return self._unified_diff(old_content, new_content, file_path)

        return None

    @staticmethod
    def _unified_diff(old: str, new: str, filename: str, context: int = 3) -> str | None:
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=context,
        )
        result = "".join(diff)
        if len(result) > 3000:
            result = result[:2500] + "\n... (diff truncated)\n"
        return result or None
