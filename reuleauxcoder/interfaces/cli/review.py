"""Single FORGE review frame shared by approvals and result history."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from reuleauxcoder.domain.approval import ApprovalSection, ApprovalSectionKind
from reuleauxcoder.interfaces.cli.terminal import build_diff_text
from reuleauxcoder.interfaces.cli.theme import CLITheme, DEFAULT_CLI_THEME
from reuleauxcoder.presentation.policy import fold_text
from reuleauxcoder.presentation.semantics import DisplayTone


def build_review_frame(
    *,
    title: str,
    summary: str,
    sections: Sequence[ApprovalSection],
    console_width: int,
    max_preview_lines: int,
    max_preview_chars: int,
    footer: Text | None = None,
    tone: DisplayTone = DisplayTone.WARNING,
    theme: CLITheme = DEFAULT_CLI_THEME,
) -> Panel:
    """Build the canonical bounded frame for any human-readable diff review."""
    renderables: list[object] = []
    if summary:
        renderables.append(
            Text(
                fold_text(
                    summary,
                    max_lines=max_preview_lines,
                    max_chars=max_preview_chars,
                ),
                style=theme.style(DisplayTone.NEUTRAL),
            )
        )
    for section in sections:
        if renderables:
            renderables.append(Text())
        renderables.append(theme.label(section.title, DisplayTone.NEUTRAL))
        if section.kind is ApprovalSectionKind.DIFF and isinstance(
            section.content, str
        ):
            renderables.append(
                build_diff_text(
                    section.content,
                    max_lines=max_preview_lines,
                    max_chars=max_preview_chars,
                    theme=theme,
                )
            )
        elif section.kind is ApprovalSectionKind.JSON and isinstance(
            section.content, Mapping
        ):
            rendered = json.dumps(
                dict(section.content), ensure_ascii=False, indent=2, default=str
            )
            renderables.append(
                Text(
                    fold_text(
                        rendered,
                        max_lines=max_preview_lines,
                        max_chars=max_preview_chars,
                    ),
                    style=theme.style(DisplayTone.NEUTRAL),
                )
            )
        else:
            renderables.append(
                Text(
                    fold_text(
                        str(section.content),
                        max_lines=max_preview_lines,
                        max_chars=max_preview_chars,
                    ),
                    style=theme.style(DisplayTone.ACCENT),
                )
            )
    if footer is not None:
        renderables.append(footer)
    return Panel(
        Group(*renderables),
        title=theme.label(title, tone),
        title_align="left",
        border_style=theme.style(tone),
        box=box.HEAVY,
        padding=(0, 1),
        width=min(100, console_width),
        expand=False,
    )
