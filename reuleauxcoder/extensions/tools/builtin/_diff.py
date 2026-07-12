"""Shared structured diff construction for file mutation tools."""

from __future__ import annotations

import difflib

from reuleauxcoder.domain.agent.tool_outcome import ToolDiff


def build_tool_diff(old: str, new: str, filename: str, *, context: int = 3) -> ToolDiff:
    """Build one unbounded unified diff and its stable line statistics."""
    normalized_old = old.replace("\r\n", "\n").replace("\r", "\n")
    normalized_new = new.replace("\r\n", "\n").replace("\r", "\n")
    unified = "".join(
        difflib.unified_diff(
            normalized_old.splitlines(keepends=True),
            normalized_new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=context,
        )
    )
    additions = 0
    deletions = 0
    for line in unified.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return ToolDiff(
        path=filename,
        unified=unified,
        additions=additions,
        deletions=deletions,
        original_chars=len(normalized_old),
    )
