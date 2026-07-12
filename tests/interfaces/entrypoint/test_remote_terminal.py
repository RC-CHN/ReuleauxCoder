from reuleauxcoder.extensions.remote_exec.protocol import TerminalCapabilities
from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    create_remote_console,
    export_remote_console,
)
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.events import UIEvent


def test_remote_console_honors_width_and_disables_ansi() -> None:
    console = create_remote_console(
        TerminalCapabilities(
            width=72,
            color_level="none",
            unicode=False,
            interactive=True,
        )
    )

    console.print("[red]plain[/red]")
    rendered = export_remote_console(console)

    assert console.width == 72
    assert rendered == "plain\n"
    assert "\x1b[" not in rendered


def test_remote_console_honors_negotiated_color() -> None:
    console = create_remote_console(
        TerminalCapabilities(width=100, color_level="truecolor")
    )

    console.print("[red]colored[/red]")
    rendered = export_remote_console(console)

    assert console.width == 100
    assert "\x1b[" in rendered


def test_remote_renderer_refreshes_negotiated_width_before_each_event() -> None:
    current_width = [72]
    console = create_remote_console(TerminalCapabilities(width=72))
    renderer = CLIRenderer(
        console_override=console,
        terminal_width_provider=lambda: current_width[0],
    )

    current_width[0] = 116
    renderer.on_ui_event(UIEvent.info("resized"))

    assert console.width == 116
