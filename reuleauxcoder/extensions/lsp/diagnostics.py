"""Core diagnostic types and rendering.

Follows the DS-TUI (DeepSeek-TUI) format:
<diagnostics file="relative/path">
  ERROR [line:col] message
  WARNING [line:col] message
</diagnostics>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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
