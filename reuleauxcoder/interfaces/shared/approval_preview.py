"""Shared diff-preview utilities for tool-approval requests.

Used by CLI and TUI approval handlers to build readable diffs
before presenting them to the user.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from reuleauxcoder.domain.approval import ApprovalRequest


def build_preview_diff(request: ApprovalRequest) -> str | None:
    """Build a unified-diff preview for file-changing approval requests.

    Handles two tool types:

    - ``edit_file``: reads the file, applies the old→new_string swap
      in memory, and diffs the result.
    - ``write_file``: diffs existing file content (or empty string if the
      file does not exist) against the proposed new content.

    Returns ``None`` when the diff cannot be constructed (missing args,
    unreadable file, ``old_string`` count != 1).  Callers should fall
    back to showing raw ``tool_args``.
    """
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

        return _unified_diff(
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

        return _unified_diff(old_content, new_content, file_path)

    return None


def _unified_diff(
    old: str, new: str, filename: str, context: int = 3
) -> str | None:
    """Compute a unified diff between *old* and *new* text.

    Truncates output to ~3 000 characters to keep approval dialogs
    readable.
    """
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
