"""Framework-neutral display semantics for transcript events.

This module decides *what* an interface should say.  CLI/TUI adapters remain
free to decide how those facts are laid out, coloured, or made interactive.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class DisplayTone(str, Enum):
    NEUTRAL = "neutral"
    MUTED = "muted"
    ACCENT = "accent"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolInvocationDisplay:
    """Stable tool-call copy shared by linear and spatial interfaces."""

    action: str
    subject: str = ""
    detail: str = ""


_TOOL_ACTIONS = {
    "shell": "RUN",
    "read_file": "READ",
    "list_file": "LIST",
    "write_file": "WRITE",
    "edit_file": "EDIT",
    "grep": "SEARCH",
    "glob": "GLOB",
    "lsp": "LSP",
    "spawn_agent": "SPAWN",
    "send_message": "MESSAGE",
    "list_agents": "AGENTS",
    "wait_agent": "WAIT",
    "interrupt_agent": "STOP",
}

_TOOL_SUBJECT_KEYS = {
    "shell": ("command",),
    "read_file": ("path", "file_path"),
    "list_file": ("path",),
    "write_file": ("path", "file_path"),
    "edit_file": ("path", "file_path"),
    "grep": ("pattern", "query"),
    "glob": ("pattern",),
    "lsp": ("operation", "file_path", "path"),
    "spawn_agent": ("message", "mode"),
    "send_message": ("job_id", "message"),
    "interrupt_agent": ("job_id",),
}


def describe_tool_invocation(
    name: str,
    arguments: Mapping[str, object] | None,
    *,
    show_arguments: bool = True,
    detail_limit: int = 80,
) -> ToolInvocationDisplay:
    """Project a tool name and arguments into terse, adapter-neutral copy."""
    args = dict(arguments or {})
    action = _TOOL_ACTIONS.get(name, name.replace("_", " ").upper())
    if not show_arguments:
        return ToolInvocationDisplay(action=action)
    subject_key = next(
        (key for key in _TOOL_SUBJECT_KEYS.get(name, ()) if args.get(key) is not None),
        None,
    )
    subject = _compact_value(args.pop(subject_key)) if subject_key else ""
    detail = _brief_arguments(args, maxlen=detail_limit)
    return ToolInvocationDisplay(action=action, subject=subject, detail=detail)


def _compact_value(value: object, *, maxlen: int = 72) -> str:
    text = str(value).replace("\n", " ").strip()
    if len(text) <= maxlen:
        return text
    return f"{text[: max(1, maxlen - 1)]}…"


def _brief_arguments(arguments: Mapping[str, object], *, maxlen: int) -> str:
    parts: list[str] = []
    for key, value in arguments.items():
        value_text = repr(value)
        if len(value_text) > 36:
            value_text = f"{value_text[:35]}…"
        part = f"{key}={value_text}"
        if len(", ".join((*parts, part))) > maxlen:
            parts.append("…")
            break
        parts.append(part)
    return ", ".join(parts)
