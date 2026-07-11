"""Central presentation verbosity and preview policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome


class Verbosity(str, Enum):
    COMPACT = "compact"
    STANDARD = "standard"
    DEBUG = "debug"


@dataclass(frozen=True)
class PresentationPolicy:
    verbosity: Verbosity = Verbosity.COMPACT
    tool_preview_chars: int = 1_200
    tool_preview_lines: int = 20

    def tool_preview(self, outcome: ToolOutcome) -> str:
        """Create a display-only preview without mutating model text."""
        if self.verbosity is Verbosity.DEBUG:
            return outcome.display_text

        text = outcome.display_text
        lines = text.splitlines()
        visible_lines = lines[: self.tool_preview_lines]
        preview = "\n".join(visible_lines)
        if len(preview) > self.tool_preview_chars:
            preview = preview[: self.tool_preview_chars]

        hidden_lines = max(0, len(lines) - len(visible_lines))
        hidden_chars = max(0, len(text) - len(preview))
        if hidden_lines or hidden_chars:
            suffix = []
            if hidden_lines:
                suffix.append(f"{hidden_lines} lines")
            if hidden_chars:
                suffix.append(f"{hidden_chars} chars")
            preview = f"{preview}\n… ({', '.join(suffix)} hidden)"
        return preview
