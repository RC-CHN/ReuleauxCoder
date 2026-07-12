"""Canonical, platform-neutral text diff construction."""

from __future__ import annotations

import difflib

from reuleauxcoder.domain.agent.tool_outcome import ToolDiff


def normalize_newlines(content: str) -> str:
    """Normalize platform line endings for comparison and presentation."""
    return content.replace("\r\n", "\n").replace("\r", "\n")


def build_unified_diff(
    old: str,
    new: str,
    *,
    fromfile: str,
    tofile: str,
    context: int = 3,
) -> str:
    """Build a stable unified diff independent of host newline conventions."""
    normalized_old = normalize_newlines(old)
    normalized_new = normalize_newlines(new)
    return "".join(
        difflib.unified_diff(
            normalized_old.splitlines(keepends=True),
            normalized_new.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            n=context,
        )
    )


def build_tool_diff(old: str, new: str, filename: str, *, context: int = 3) -> ToolDiff:
    """Build one unbounded tool diff and its stable line statistics."""
    normalized_old = normalize_newlines(old)
    unified = build_unified_diff(
        old,
        new,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        context=context,
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
