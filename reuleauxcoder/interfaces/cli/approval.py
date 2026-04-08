"""CLI approval interaction provider."""

from __future__ import annotations

from prompt_toolkit import prompt as pt_prompt

from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalProvider, ApprovalRequest
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


class CLIApprovalProvider(ApprovalProvider):
    """Minimal interactive approval provider for CLI mode."""

    def __init__(self, ui_bus: UIEventBus):
        self.ui_bus = ui_bus

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.ui_bus.warning(
            f"Approval required for tool '{request.tool_name}' ({request.tool_source})",
            kind=UIEventKind.COMMAND,
        )
        if request.reason:
            self.ui_bus.info(f"Reason: {request.reason}", kind=UIEventKind.COMMAND)
        if request.tool_args:
            self.ui_bus.info(
                f"Args: {request.tool_args}",
                kind=UIEventKind.COMMAND,
            )

        while True:
            answer = pt_prompt("Approve tool execution? [y/n]: ").strip().lower()
            if answer in {"y", "yes"}:
                return ApprovalDecision.allow_once("approved in CLI")
            if answer in {"n", "no"}:
                return ApprovalDecision.deny_once("denied in CLI")
            self.ui_bus.warning("Please enter 'y' or 'n'.", kind=UIEventKind.COMMAND)
