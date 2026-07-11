"""Shared Rich presenter for local and remote CLI interactions."""

from __future__ import annotations

from collections.abc import Mapping

from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
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


def render_interaction_request(console: Console, request: InteractionRequest) -> None:
    """Render adapter-neutral interaction facts with one terminal policy."""
    if isinstance(request, ConfirmRequest):
        console.print(Panel(request.message, title=request.title, border_style="yellow"))
        return
    if isinstance(request, ChooseOneRequest):
        table = Table(title=request.title, show_header=True)
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
        console.print(Panel(request.prompt, title=request.title, border_style="blue"))
        return
    if isinstance(request, ReviewRequest):
        console.print(
            Panel(request.summary, title=request.title, border_style="yellow")
        )
        for section in request.sections:
            if (
                section.kind is ApprovalSectionKind.DIFF
                and isinstance(section.content, str)
            ):
                console.print(f"[bold]{section.title}[/bold]")
                render_diff_panel(section.content, console)
            elif (
                section.kind is ApprovalSectionKind.JSON
                and isinstance(section.content, Mapping)
            ):
                console.print(
                    Panel(JSON.from_data(dict(section.content)), title=section.title)
                )
            else:
                console.print(Panel(str(section.content), title=section.title))


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
