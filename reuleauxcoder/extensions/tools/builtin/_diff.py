"""Shared structured diff construction for file mutation tools."""

from __future__ import annotations

import difflib

from reuleauxcoder.domain.agent.tool_outcome import ToolDiff


def build_tool_diff(
    old: str, new: str, filename: str, *, context: int = 3
) -> ToolDiff:
    """Build one unbounded unified diff and its stable line statistics."""
    unified = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
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
        original_chars=len(old),
    )
