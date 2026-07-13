"""FORGE startup plate, isolated from the runtime history renderer."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from reuleauxcoder.interfaces.cli.theme import CLITheme, DEFAULT_CLI_THEME
from reuleauxcoder.interfaces.events import UIEvent, UIEventLevel
from reuleauxcoder.presentation.semantics import DisplayTone


def show_banner(
    model: str,
    base_url: str | None,
    version: str,
    *,
    console_override: Console | None = None,
    startup_events: Sequence[UIEvent] = (),
    theme: CLITheme = DEFAULT_CLI_THEME,
) -> None:
    """Render the sole persistent box in CLI scrollback: the session plate."""
    from reuleauxcoder.infrastructure.platform import get_platform_info

    target = console_override or Console()
    platform_info = get_platform_info()
    shell = platform_info.get_preferred_shell()
    panel_width = min(88, target.width)
    value_width = max(8, panel_width - 18)
    body = Text()
    body.append_text(theme.label("FORGE", DisplayTone.ACCENT))
    body.append("  REULEAUXCODER", style="bold")
    body.append(f"  //  V{version}", style=theme.style(DisplayTone.MUTED))
    body.append("\n")
    _fact(body, "MODEL", _truncate_middle(model, value_width), theme)
    _fact(body, "ROOT", _truncate_middle(str(Path.cwd()), value_width), theme)
    _fact(body, "RUNTIME", f"{platform_info.system.upper()} / {shell.value}", theme)
    if base_url:
        _fact(body, "BASE", _truncate_middle(base_url, value_width), theme)

    visible = [
        event for event in startup_events if event.level is not UIEventLevel.DEBUG
    ]
    if visible:
        body.append("\n")
    for index, event in enumerate(visible):
        tone = _event_tone(event.level)
        label = {
            UIEventLevel.INFO: "INIT",
            UIEventLevel.SUCCESS: "READY",
            UIEventLevel.WARNING: "WARN",
            UIEventLevel.ERROR: "ERROR",
            UIEventLevel.DEBUG: "DEBUG",
        }[event.level]
        lines = event.message.splitlines() or [""]
        body.append_text(theme.label(label, tone))
        body.append(f" {lines[0]}", style=theme.style(tone))
        for line in lines[1:]:
            body.append("\n         ")
            body.append(line, style=theme.style(tone))
        if index < len(visible) - 1:
            body.append("\n")

    target.print(
        Panel(
            body,
            border_style=theme.frame,
            width=panel_width,
            expand=False,
            padding=(0, 1),
            title="[bold] SESSION PLATE [/bold]",
            title_align="left",
        )
    )
    footer = Text(" COMMAND ", style=theme.label_styles[DisplayTone.MUTED])
    footer.append(" /help  //  Ctrl+C cancel  //  /quit exit", style="bold")
    target.print(footer)


def _fact(body: Text, label: str, value: str, theme: CLITheme) -> None:
    body.append("\n")
    body.append(f"{label:<8}", style=theme.style(DisplayTone.MUTED))
    body.append(value)


def _event_tone(level: UIEventLevel) -> DisplayTone:
    return {
        UIEventLevel.INFO: DisplayTone.NEUTRAL,
        UIEventLevel.SUCCESS: DisplayTone.SUCCESS,
        UIEventLevel.WARNING: DisplayTone.WARNING,
        UIEventLevel.ERROR: DisplayTone.ERROR,
        UIEventLevel.DEBUG: DisplayTone.MUTED,
    }[level]


def _truncate_middle(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    left = (width - 1 + 1) // 2
    right = width - 1 - left
    return f"{value[:left]}…{value[-right:]}" if right else f"{value[:left]}…"
