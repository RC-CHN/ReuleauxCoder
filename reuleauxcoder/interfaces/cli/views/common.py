"""Common helpers for CLI structured view rendering."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from reuleauxcoder.presentation.semantics import DisplayTone


def stop_stream_and_clear(renderer) -> None:
    """Finalize any active stream before rendering a structured view."""
    close_active = getattr(renderer, "_close_active_content_block", None)
    if callable(close_active):
        close_active()


def render_markdown_panel(
    renderer, *, markdown_text: str, title: str, border_style: str = "blue"
) -> bool:
    """Render titled markdown without width-dependent box borders."""
    if not markdown_text:
        return False
    stop_stream_and_clear(renderer)
    render_heading(renderer, title)
    renderer.console.print(Markdown(markdown_text))
    return True


def render_heading(
    renderer, title: str, tone: DisplayTone = DisplayTone.ACCENT
) -> None:
    renderer.console.print(renderer.theme.label(escape(title), tone))


def render_notice(renderer, message: str, tone: DisplayTone) -> None:
    label = {
        DisplayTone.SUCCESS: "OK",
        DisplayTone.WARNING: "WARN",
        DisplayTone.ERROR: "ERROR",
    }.get(tone, "INFO")
    row = Text()
    row.append_text(renderer.theme.label(label, tone))
    row.append(f" {message}", style=renderer.theme.style(tone))
    renderer.console.print(row, soft_wrap=True)


def make_table(renderer, *args, **kwargs) -> Table:
    kwargs.setdefault("box", None)
    kwargs.setdefault("pad_edge", False)
    kwargs.setdefault("header_style", renderer.theme.style(DisplayTone.ACCENT))
    kwargs.setdefault("title_style", "bold")
    return Table(*args, **kwargs)
