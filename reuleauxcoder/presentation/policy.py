"""Central presentation verbosity and preview policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome


class Verbosity(str, Enum):
    COMPACT = "compact"
    STANDARD = "standard"
    DEBUG = "debug"


class ToolOutputMode(str, Enum):
    ERRORS = "errors"
    SUMMARY = "summary"
    PREVIEW = "preview"
    FULL = "full"


class ReasoningDisplay(str, Enum):
    HIDDEN = "hidden"
    INDICATOR = "indicator"
    INLINE = "inline"


class NotificationThreshold(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class PresentationPolicy:
    verbosity: Verbosity = Verbosity.COMPACT
    tool_output_mode: ToolOutputMode = ToolOutputMode.SUMMARY
    tool_preview_chars: int = 1_200
    tool_preview_lines: int = 20
    show_tool_args: bool = True
    reasoning_display: ReasoningDisplay = ReasoningDisplay.INDICATOR
    notification_threshold: NotificationThreshold = NotificationThreshold.INFO

    @classmethod
    def from_ui_config(cls, config) -> "PresentationPolicy":
        return cls(
            verbosity=Verbosity(config.verbosity),
            tool_output_mode=ToolOutputMode(config.tool_output),
            tool_preview_chars=config.max_preview_chars,
            tool_preview_lines=config.max_preview_lines,
            show_tool_args=config.show_tool_args,
            reasoning_display=ReasoningDisplay(config.reasoning_display),
            notification_threshold=NotificationThreshold(
                config.notification_threshold
            ),
        )

    def tool_preview(self, outcome: ToolOutcome) -> str:
        """Create a display-only preview without mutating model text."""
        if self.tool_output_mode is ToolOutputMode.ERRORS and outcome.success:
            return ""
        if self.tool_output_mode is ToolOutputMode.FULL:
            return outcome.ui_text(include_details=True)

        text = outcome.ui_text(
            include_details=self.tool_output_mode is ToolOutputMode.PREVIEW
        )
        if self.tool_output_mode is ToolOutputMode.SUMMARY:
            return text
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

    def should_render_notification(self, level: str) -> bool:
        rank = {"debug": 0, "info": 1, "success": 1, "warning": 2, "error": 3}
        return rank.get(level, 1) >= rank[self.notification_threshold.value]
