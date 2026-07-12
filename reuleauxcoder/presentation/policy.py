"""Central presentation verbosity and preview policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome, ToolOutcomeStatus


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

        if outcome.status in {
            ToolOutcomeStatus.TIMED_OUT,
            ToolOutcomeStatus.CANCELLED,
        }:
            lines = outcome.ui_text(include_details=True).splitlines()
            if not lines:
                return ""
            return "\n".join([*lines[:-1][-5:], lines[-1]])

        if self.tool_output_mode is ToolOutputMode.SUMMARY and outcome.summary:
            return outcome.summary

        # Legacy tools do not always provide a summary. They must still obey the
        # UI budget: model-side truncation is deliberately independent and the
        # structured outcome may retain the complete source text.
        text = outcome.ui_text(include_details=True)
        return fold_text(
            text,
            max_lines=self.tool_preview_lines,
            max_chars=self.tool_preview_chars,
        )

    def should_render_notification(self, level: str) -> bool:
        rank = {"debug": 0, "info": 1, "success": 1, "warning": 2, "error": 3}
        return rank.get(level, 1) >= rank[self.notification_threshold.value]

    def tool_diff_preview(self, outcome: ToolOutcome) -> str:
        """Return a bounded, separately renderable default review diff."""
        if (
            self.tool_output_mode is not ToolOutputMode.SUMMARY
            or outcome.diff is None
            or not outcome.metadata.get("show_diff_by_default")
            or outcome.metadata.get("diff_reviewed")
        ):
            return ""
        return fold_text(
            outcome.diff.unified,
            max_lines=self.tool_preview_lines,
            max_chars=self.tool_preview_chars,
        )


def fold_text(text: str, *, max_lines: int, max_chars: int) -> str:
    """Return a bounded head+tail projection suitable for terminal scrollback.

    The source string is never mutated. ``full`` presentation remains the
    explicit escape hatch for users who intentionally want unbounded output.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text

    line_budget = max(1, max_lines)
    head_lines = max(1, (line_budget + 1) // 2)
    tail_lines = max(0, line_budget - head_lines)
    selected = lines[:head_lines]
    if tail_lines:
        selected.extend(lines[-tail_lines:])
    selected_text = "\n".join(selected)
    marker = (
        f"… (output folded; {len(lines)} lines, {len(text)} chars total; "
        "set ui.tool_output=full to show all)"
    )
    char_budget = max(1, max_chars)
    if len(selected_text) <= char_budget:
        if tail_lines:
            return (
                "\n".join(lines[:head_lines])
                + f"\n{marker}\n"
                + "\n".join(lines[-tail_lines:])
            )
        return f"{selected_text}\n{marker}"

    head_chars = max(1, (char_budget + 1) // 2)
    tail_chars = max(0, char_budget - head_chars)
    head = selected_text[:head_chars].rstrip()
    tail = selected_text[-tail_chars:].lstrip() if tail_chars else ""
    return f"{head}\n{marker}\n{tail}" if tail else f"{head}\n{marker}"
