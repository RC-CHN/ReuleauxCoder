"""Small Rich terminal rendering primitives shared by CLI presenters."""

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax


def render_diff_panel(result: str, console: Console) -> None:
    try:
        syntax = Syntax(result, "diff", theme="monokai", line_numbers=False)
        console.print(Panel(syntax, border_style="green", padding=(0, 1)))
    except Exception:
        console.print(f"[dim]{result[:500]}[/dim]")
