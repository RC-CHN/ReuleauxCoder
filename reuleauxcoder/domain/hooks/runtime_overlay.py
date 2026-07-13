"""Shared helpers for bounded request-time execution overlay extensions."""

from __future__ import annotations

from typing import Any


_RUNTIME_MARKER = "<runtime_instruction>"


def has_runtime_overlay_tail(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False
    tail = messages[-1]
    content = str(tail.get("content") or "")
    return (
        tail.get("role") == "system"
        and content.startswith("<execution_state")
        and _RUNTIME_MARKER in content
    )


def inject_runtime_overlay_region(messages: list[dict[str, Any]], region: str) -> bool:
    """Insert one already-sanitized region before the trusted instruction."""
    if not has_runtime_overlay_tail(messages):
        return False
    tail = messages[-1]
    content = str(tail.get("content") or "")
    tail["content"] = content.replace(_RUNTIME_MARKER, region + _RUNTIME_MARKER, 1)
    return True
