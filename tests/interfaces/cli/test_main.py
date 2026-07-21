from io import StringIO
from types import SimpleNamespace

from rich.console import Console

import reuleauxcoder.interfaces.cli.main as main_module
from reuleauxcoder.presentation.semantics import DisplayTone


def test_keyboard_interrupt_during_initialize_exits_without_traceback(
    monkeypatch, capsys
) -> None:
    cleaned_up = []

    class InterruptingRunner:
        def __init__(self, _options, *, startup_progress=None) -> None:
            pass

        def initialize(self):
            raise KeyboardInterrupt

        def cleanup(self) -> None:
            cleaned_up.append(True)

    monkeypatch.setattr(
        main_module,
        "parse_args",
        lambda: SimpleNamespace(
            config=None,
            model=None,
            resume=None,
            server=False,
        ),
    )
    monkeypatch.setattr(main_module, "AppRunner", InterruptingRunner)

    assert main_module.main() == 130
    assert cleaned_up == [True]
    assert capsys.readouterr().err == "Interrupted.\n"


def test_terminal_status_flushes_progress_to_stderr(capsys) -> None:
    main_module._terminal_status("Reading history ledger (12.0 MB)...")

    assert capsys.readouterr().err == (
        "rcoder: Reading history ledger (12.0 MB)...\n"
    )


def test_terminal_status_uses_theme_colors_on_a_color_terminal() -> None:
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        width=100,
    )

    main_module._terminal_status(
        "Session saved: session-1.",
        tone=DisplayTone.SUCCESS,
        console_override=console,
    )

    rendered = stream.getvalue()
    assert "\x1b[" in rendered
    assert "rcoder" in rendered
    assert "Session saved: session-1." in rendered
