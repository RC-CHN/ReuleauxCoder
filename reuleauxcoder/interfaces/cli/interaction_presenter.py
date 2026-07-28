"""Shared Rich presenter for local and remote CLI interactions."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from reuleauxcoder.interfaces.interactions import (
    ChooseOneRequest,
    ConfirmRequest,
    InputTextRequest,
    InteractionRequest,
    ReviewRequest,
)
from reuleauxcoder.interfaces.cli.review import build_review_frame
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
        choices = Text("\n")
        choices.append("[1/Y] ", style=theme.style(DisplayTone.SUCCESS))
        choices.append(request.approve_label, style="bold")
        if request.grant_options:
            choices.append("    ")
            choices.append("[S] ", style=theme.style(DisplayTone.WARNING))
            choices.append("Allow for session…", style="bold")
        choices.append("    ")
        choices.append("[2/N] ", style=theme.style(DisplayTone.ERROR))
        choices.append(request.reject_label, style="bold")
        choices.append("    ")
        choices.append("[F] ", style=theme.style(DisplayTone.ERROR))
        choices.append("Deny with feedback", style="bold")
        choices.append(
            "\nSELECT AN ACTION // CTRL+C CANCELS",
            style=theme.style(DisplayTone.MUTED),
        )
        console.print(
            build_review_frame(
                title=request.title,
                summary=request.summary,
                sections=request.sections,
                console_width=console.width,
                max_preview_lines=max_preview_lines,
                max_preview_chars=max_preview_chars,
                footer=choices,
                theme=theme,
            )
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
    console.print(bounded, markup=False, highlight=False, soft_wrap=True)


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
            "secret": request.secret,
        }
    constraints: dict[str, object] = {
        "value_type": "review_decision",
        "approve_label": request.approve_label,
        "reject_label": request.reject_label,
        "actions": ("allow_once", "allow_session", "deny"),
        "supports_feedback": True,
    }
    if request.grant_options:
        constraints["grant_options"] = tuple(
            {
                "id": option.id,
                "label": option.label,
                "description": option.description,
                "broad": option.broad,
            }
            for option in request.grant_options
        )
    return constraints
