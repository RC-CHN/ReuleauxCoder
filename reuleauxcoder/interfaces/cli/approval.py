"""CLI approval interaction provider."""

from __future__ import annotations

import difflib
from pathlib import Path

from reuleauxcoder.domain.approval import ApprovalDecision, ApprovalProvider, ApprovalRequest
from reuleauxcoder.interfaces.interactions import ReviewRequest, UIInteractor


class CLIApprovalProvider(ApprovalProvider):
    """Approval provider backed by the shared UIInteractor."""

    def __init__(self, ui_interactor: UIInteractor):
        self.ui_interactor = ui_interactor

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        sections: list[dict] = []
        diff_text = self._build_preview_diff(request)
        if diff_text is not None:
            title = "Proposed file diff" if request.tool_name == "write_file" else "Proposed edit diff"
            sections.append({"id": "diff", "title": title, "kind": "diff", "content": diff_text})
        elif request.tool_args:
            sections.append({"id": "args", "title": "Arguments", "kind": "json", "content": request.tool_args})

        response = self.ui_interactor.review(
            ReviewRequest(
                title=f"Approval required: {request.tool_name}",
                summary=(
                    f"Tool '{request.tool_name}' from source '{request.tool_source}' requires approval."
                ),
                sections=sections,
                metadata={
                    "tool_name": request.tool_name,
                    "tool_source": request.tool_source,
                    "reason": request.reason,
                    **request.metadata,
                },
            )
        )
        if response.approved:
            return ApprovalDecision.allow_once(response.reason or "approved via UI interactor")
        return ApprovalDecision.deny_once(response.reason or "denied via UI interactor")

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

