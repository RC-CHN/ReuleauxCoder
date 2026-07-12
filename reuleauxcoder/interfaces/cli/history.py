"""Linear Rich adapter for runtime history using the FORGE theme."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.approval import ApprovalSection, ApprovalSectionKind
from reuleauxcoder.domain.runtime.events import SubagentFinished
from reuleauxcoder.interfaces.cli.review import build_review_frame
from reuleauxcoder.interfaces.cli.theme import CLITheme, DEFAULT_CLI_THEME
from reuleauxcoder.presentation import PresentationPolicy, describe_tool_invocation
from reuleauxcoder.presentation.policy import fold_text
from reuleauxcoder.presentation.semantics import DisplayTone


class CLIHistoryPresenter:
    """Render immutable history rows; owns no runtime or transcript state."""

    def __init__(
        self,
        console: Console,
        policy: PresentationPolicy,
        theme: CLITheme = DEFAULT_CLI_THEME,
    ) -> None:
        self.console = console
        self.policy = policy
        self.theme = theme

    def tool_started(self, name: str, arguments: dict | None) -> None:
        display = describe_tool_invocation(
            name,
            arguments,
            show_arguments=self.policy.show_tool_args,
            detail_limit=max(16, self.console.width - 32),
        )
        row = Text()
        row.append_text(self.theme.label(display.action))
        if display.subject:
            row.append(" ")
            row.append(display.subject, style="bold")
        if display.detail:
            row.append("  ")
            row.append(display.detail, style=self.theme.style(DisplayTone.MUTED))
        self.console.print(row, soft_wrap=True)

    def tool_finished(self, name: str, outcome: ToolOutcome) -> None:
        display = self.policy.tool_preview(outcome)
        if not display:
            return
        if not outcome.success:
            row = Text()
            row.append_text(self.theme.label("FAIL", DisplayTone.ERROR))
            row.append(f" {name}  ")
            row.append(display, style=self.theme.style(DisplayTone.ERROR))
            self.console.print(row, soft_wrap=True)
            return

        diff = self.policy.tool_diff_preview(outcome)
        if diff:
            operation = str(outcome.metadata.get("operation") or name).upper()
            self.console.print(
                build_review_frame(
                    title=f"{operation} RESULT",
                    summary=display,
                    sections=(
                        ApprovalSection(
                            id="diff",
                            title="Applied diff",
                            kind=ApprovalSectionKind.DIFF,
                            content=diff,
                        ),
                    ),
                    console_width=self.console.width,
                    max_preview_lines=self.policy.tool_preview_lines,
                    max_preview_chars=self.policy.tool_preview_chars,
                    tone=DisplayTone.ACCENT,
                    theme=self.theme,
                )
            )
            return
        row = Text(" └ ", style=self.theme.style(DisplayTone.MUTED))
        row.append(display, style=self.theme.style(DisplayTone.MUTED))
        self.console.print(row, soft_wrap=True)

    def subagent_finished(self, payload: SubagentFinished) -> None:
        tone = DisplayTone.ERROR if payload.error else DisplayTone.ACCENT
        row = Text()
        row.append_text(self.theme.label("AGENT", tone))
        row.append(f" {payload.job_id}  {payload.mode}  {payload.status}")
        if payload.error:
            error = fold_text(
                payload.error,
                max_lines=self.policy.tool_preview_lines,
                max_chars=self.policy.tool_preview_chars,
            )
            row.append(f"  {error}", style=self.theme.style(DisplayTone.ERROR))
        self.console.print(row, soft_wrap=True)

    def notice(self, message: str, *, level: str, category: str = "") -> None:
        tone = {
            "success": DisplayTone.SUCCESS,
            "warning": DisplayTone.WARNING,
            "error": DisplayTone.ERROR,
            "debug": DisplayTone.MUTED,
        }.get(level, DisplayTone.MUTED)
        label = {
            "success": "OK",
            "warning": "WARN",
            "error": "ERROR",
            "debug": "DEBUG",
        }.get(level, "INFO")
        row = Text()
        row.append_text(self.theme.label(label, tone))
        row.append(" ")
        if category and category != "system":
            row.append(f"{category.upper()} // ", style=self.theme.style(tone))
        row.append(message, style=self.theme.style(tone))
        self.console.print(row, soft_wrap=True)

    def reasoning_indicator(self) -> None:
        row = Text()
        row.append_text(self.theme.label("THINK", DisplayTone.MUTED))
        row.append(" processing", style=self.theme.style(DisplayTone.MUTED))
        self.console.print(row)

    def reasoning_prefix(self) -> None:
        row = Text()
        row.append_text(self.theme.label("THINK", DisplayTone.MUTED))
        row.append(" ")
        self.console.print(row, end="")
