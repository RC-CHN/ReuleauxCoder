"""Shared Rich presenter for local and remote CLI interactions."""

from __future__ import annotations

from collections.abc import Mapping
import json

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from reuleauxcoder.domain.approval import ApprovalSectionKind
from reuleauxcoder.interfaces.interactions import (
    ChooseOneRequest,
    ConfirmRequest,
    InputTextRequest,
    InteractionRequest,
    ReviewRequest,
)
from reuleauxcoder.interfaces.cli.terminal import render_diff_panel
from reuleauxcoder.interfaces.cli.theme import CLITheme, DEFAULT_CLI_THEME
from reuleauxcoder.presentation.policy import fold_text
from reuleauxcoder.presentation.semantics import DisplayTone


def render_interaction_request(
    console: Console,
    request: InteractionRequest,
    *,
    max_preview_lines: int = 20,
    max_preview_chars: int = 1_200,
    theme: CLITheme = DEFAULT_CLI_THEME,
) -> None:
    """Render adapter-neutral facts without resize-fragile box borders."""
    if isinstance(request, ConfirmRequest):
        _heading(console, request.title, theme=theme, tone=DisplayTone.WARNING)
        _body(console, request.message, max_preview_lines, max_preview_chars)
        return
    if isinstance(request, ChooseOneRequest):
        table = Table(
            title=theme.label(request.title, DisplayTone.ACCENT),
            show_header=True,
            box=None,
            pad_edge=False,
            header_style=theme.style(DisplayTone.ACCENT),
        )
        table.add_column("#", justify="right")
        table.add_column("Choice")
        table.add_column("Description")
        for index, item in enumerate(request.items, 1):
            table.add_row(str(index), item.label, item.description or "")
        console.print(table)
        if request.message:
            console.print(request.message)
        return
    if isinstance(request, InputTextRequest):
        _heading(console, request.title, theme=theme)
        _body(console, request.prompt, max_preview_lines, max_preview_chars)
        return
    if isinstance(request, ReviewRequest):
        _heading(console, request.title, theme=theme, tone=DisplayTone.WARNING)
        _body(console, request.summary, max_preview_lines, max_preview_chars)
        for section in request.sections:
            _heading(console, section.title, theme=theme, tone=DisplayTone.NEUTRAL)
            if (
                section.kind is ApprovalSectionKind.DIFF
                and isinstance(section.content, str)
            ):
                render_diff_panel(
                    section.content,
                    console,
                    max_lines=max_preview_lines,
                    max_chars=max_preview_chars,
                    theme=theme,
                )
            elif (
                section.kind is ApprovalSectionKind.JSON
                and isinstance(section.content, Mapping)
            ):
                rendered = json.dumps(
                    dict(section.content),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                _body(console, rendered, max_preview_lines, max_preview_chars)
            else:
                _body(
                    console,
                    str(section.content),
                    max_preview_lines,
                    max_preview_chars,
                )
        console.print()
        console.print(f"  [1/Y] {escape(request.approve_label)}")
        console.print(f"  [2/N] {escape(request.reject_label)}")
        console.print(
            "  SELECT 1/2 OR Y/N // CTRL+C CANCELS",
            style=theme.style(DisplayTone.MUTED),
        )


def _heading(
    console: Console,
    title: str,
    *,
    theme: CLITheme,
    tone: DisplayTone = DisplayTone.ACCENT,
) -> None:
    row = theme.label(title, tone)
    console.print(row)


def _body(console: Console, text: str, max_lines: int, max_chars: int) -> None:
    bounded = fold_text(text, max_lines=max_lines, max_chars=max_chars)
    console.print(bounded, markup=False, soft_wrap=True)


def interaction_constraints(request: InteractionRequest) -> dict[str, object]:
    """Return the opaque wire constraints understood by the Remote CLI."""
    if isinstance(request, ConfirmRequest):
        return {"value_type": "boolean"}
    if isinstance(request, ChooseOneRequest):
        return {
            "value_type": "choice_id",
            "choices": tuple(item.id for item in request.items),
            "allow_cancel": request.allow_cancel,
        }
    if isinstance(request, InputTextRequest):
        return {
            "value_type": "string",
            "allow_empty": request.allow_empty,
        }
    return {
        "value_type": "boolean",
        "approve_label": request.approve_label,
        "reject_label": request.reject_label,
    }
