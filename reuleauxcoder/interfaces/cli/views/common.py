"""Common helpers for CLI structured view rendering."""

from __future__ import annotations

from rich.markdown import Markdown
from rich.panel import Panel


def stop_stream_and_clear(renderer) -> None:
    """Finalize any active stream before rendering a structured view."""
    if renderer._streamed_tokens:
        renderer._stop_live_markdown(render_final=True)
        renderer._streamed_tokens.clear()


def render_markdown_panel(renderer, *, markdown_text: str, title: str, border_style: str = "blue") -> bool:
    """Render markdown content in a standard CLI panel."""
    if not markdown_text:
        return False
    stop_stream_and_clear(renderer)
    renderer.console.print(Panel(Markdown(markdown_text), title=title, border_style=border_style))
    return True
