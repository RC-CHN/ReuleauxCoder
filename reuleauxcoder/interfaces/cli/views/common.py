"""Common helpers for CLI structured view rendering."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.markup import escape


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
    renderer.console.print(
        f"[bold {border_style}]{escape(title)}[/bold {border_style}]"
    )
    renderer.console.print(Markdown(markdown_text))
    return True
