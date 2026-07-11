"""Core diagnostic types and rendering.

Follows the DS-TUI (DeepSeek-TUI) format:
<diagnostics file="relative/path">
  ERROR [line:col] message
  WARNING [line:col] message
</diagnostics>
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# LSP DiagnosticSeverity constants
SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFORMATION = 3
SEVERITY_HINT = 4

_SEVERITY_LABELS: dict[int, str] = {
    SEVERITY_ERROR: "ERROR",
    SEVERITY_WARNING: "WARNING",
    SEVERITY_INFORMATION: "INFO",
    SEVERITY_HINT: "HINT",
}


@dataclass(slots=True)
class Diagnostic:
    """A single LSP diagnostic with 1-based line/character positions."""

    line: int
    character: int
    message: str
    severity: int = SEVERITY_ERROR
    code: str | None = None

    @property
    def severity_label(self) -> str:
        return _SEVERITY_LABELS.get(self.severity, "UNKNOWN")

    @property
    def is_error(self) -> bool:
        return self.severity == SEVERITY_ERROR

    @property
    def is_warning(self) -> bool:
        return self.severity == SEVERITY_WARNING


@dataclass(slots=True)
class DiagnosticBlock:
    """Diagnostics for a single file, ready for rendering."""

    file_path: str  # workspace-relative path
    items: list[Diagnostic] = field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.items) == 0


@dataclass(frozen=True, slots=True)
class DiagnosticRoute:
    """Ownership coordinates for one diagnostics request.

    Optional values describe an explicitly unknown boundary; they are never
    treated as wildcards when two concrete routes are compared.  Keeping the
    coordinates on the batch prevents a late worker result from leaking into
    another agent, session generation, turn, tool call, or document.
    """

    file_path: Path
    agent_id: str | None = None
    session_generation: int | None = None
    session_id: str | None = None
    turn_id: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticRouteFilter:
    """Partial ownership coordinates used only for batch selection."""

    file_path: Path | None = None
    agent_id: str | None = None
    session_generation: int | None = None
    session_id: str | None = None
    turn_id: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticBatch:
    """One publishDiagnostics observation, including an explicit clean state."""

    route: DiagnosticRoute
    request_sequence: int
    document_version: int
    diagnostic_generation: int
    block: DiagnosticBlock
    batch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)


def render_blocks(
    blocks: list[DiagnosticBlock],
    *,
    max_diagnostics: int = 20,
    include_warnings: bool = False,
) -> str | None:
    """Render diagnostic blocks into XML format for LLM context injection.

    Args:
        blocks: List of DiagnosticBlock to render.
        max_diagnostics: Max items per file (extra are silently dropped).
        include_warnings: If False, only ERROR severity items are included.

    Returns:
        Rendered XML string, or None if all blocks are empty after filtering.
    """
    parts: list[str] = []

    for block in blocks:
        items = block.items
        if not include_warnings:
            items = [d for d in items if d.is_error]

        if not items:
            continue

        # Cap per file
        items = items[:max_diagnostics]

        # Sort: errors first, then by line
        items = sorted(items, key=lambda d: (d.severity, d.line))

        lines: list[str] = [f'<diagnostics file="{block.file_path}">']
        for d in items:
            # Trim to first line for compactness
            msg = d.message.split("\n")[0]
            lines.append(f"  {d.severity_label} [{d.line}:{d.character}] {msg}")
        lines.append("</diagnostics>")

        parts.append("\n".join(lines))

    if not parts:
        return None

    return "\n\n".join(parts)
