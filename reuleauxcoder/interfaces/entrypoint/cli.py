"""Local CLI composition: owns backend startup, connection and cleanup."""

import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from reuleauxcoder.interfaces.cli.args import parse_args
from reuleauxcoder.interfaces.cli.registration import create_cli_registration
from reuleauxcoder.interfaces.cli.application import run_cli
from reuleauxcoder.interfaces.cli.theme import DEFAULT_CLI_THEME
from reuleauxcoder.interfaces.entrypoint import AppRunner, AppOptions
from reuleauxcoder.presentation.semantics import DisplayTone
from reuleauxcoder.domain.context.manager import (
    has_cached_tiktoken_vocabulary,
    prepare_tiktoken_encoder,
)
from reuleauxcoder.services.config.loader import ExampleConfigError
from reuleauxcoder.infrastructure.persistence.session_store import SessionRestoreError


def _terminal_status(
    message: str,
    *,
    tone: DisplayTone = DisplayTone.NEUTRAL,
    console_override: Console | None = None,
) -> None:
    """Render styled status while no interactive renderer owns the terminal."""
    target = console_override or Console(
        file=sys.stderr,
        highlight=False,
        soft_wrap=True,
    )
    line = Text()
    line.append("rcoder", style=DEFAULT_CLI_THEME.style(DisplayTone.ACCENT))
    line.append(": ", style=DEFAULT_CLI_THEME.style(DisplayTone.MUTED))
    line.append(message, style=DEFAULT_CLI_THEME.style(tone))
    target.print(line, soft_wrap=True)


def main():
    """CLI main entry point."""
    args = parse_args()

    # Build options from CLI args
    options = AppOptions(
        config_path=Path(args.config) if args.config else None,
        model=args.model,
        resume_session_id=args.resume,
        auto_resume_latest=True,
        server_mode=args.server,
    )
    startup_progress_active = True

    def report_startup(message: str) -> None:
        if startup_progress_active:
            _terminal_status(message, tone=DisplayTone.NEUTRAL)

    startup_progress = (
        report_startup
        if not getattr(args, "prompt", None)
        and not args.server
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        else None
    )

    # Initialize application using shared entrypoint
    runner = None
    try:
        if startup_progress is not None and not has_cached_tiktoken_vocabulary():
            prepare_tiktoken_encoder(progress=report_startup)
        runner = AppRunner(options, startup_progress=startup_progress)
        ctx = runner.initialize()
        startup_progress_active = False
    except ExampleConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except SessionRestoreError as error:
        if runner is not None:
            try:
                runner.cleanup()
            except Exception:
                pass
        _terminal_status(str(error), tone=DisplayTone.ERROR)
        return 1
    except KeyboardInterrupt:
        if runner is not None:
            try:
                runner.cleanup()
            except Exception:
                pass
        print("Interrupted.", file=sys.stderr)
        return 130
    except BaseException:
        if runner is not None:
            runner.cleanup()
        raise

    from reuleauxcoder.app.ui_events import UIEventBus
    from reuleauxcoder.interfaces.entrypoint.rpc import connect_local

    frontend_bus = UIEventBus()
    cli_ui = create_cli_registration(frontend_bus)
    connection = None
    try:
        connection = connect_local(
            ctx,
            cli_ui.profile,
            frontend_bus,
            cli_ui.interactor,
            foreground_interactions=True,
        )
        return run_cli(
            connection.client,
            frontend_bus,
            cli_ui,
            prompt=args.prompt,
            history_file=ctx.config.history_file,
        )
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            cli_ui.interactor.shutdown()
            runner.cleanup()
