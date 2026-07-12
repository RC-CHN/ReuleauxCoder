"""Transient FORGE activity pulse for terminal-only live feedback."""

from __future__ import annotations

from collections.abc import Callable
import time

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.text import Text

from reuleauxcoder.interfaces.cli.theme import CLITheme, DEFAULT_CLI_THEME
from reuleauxcoder.presentation.semantics import DisplayTone

_PHASES = ("·  ", "·· ", "···")


class _PulseRenderable:
    def __init__(
        self,
        label: str,
        detail: str,
        *,
        timed: bool,
        theme: CLITheme,
    ) -> None:
        self.label = label
        self.detail = detail
        self.timed = timed
        self.theme = theme
        self.phase = 0
        self.started_at = time.monotonic()

    def bump(self) -> None:
        self.phase = (self.phase + 1) % len(_PHASES)

    def __rich_console__(
        self, console: Console, options: ConsoleOptions  # noqa: ARG002
    ) -> RenderResult:
        phase = (
            int((time.monotonic() - self.started_at) * 4) % len(_PHASES)
            if self.timed
            else self.phase
        )
        row = Text()
        row.append_text(self.theme.label(self.label, DisplayTone.MUTED))
        row.append(f" {_PHASES[phase]}", style=self.theme.style(DisplayTone.ACCENT))
        if self.detail:
            row.append(f" {self.detail}", style=self.theme.style(DisplayTone.MUTED))
        yield row


class CLIActivityPresenter:
    """Own the single transient live region used by the append-only CLI."""

    def __init__(
        self,
        console: Console,
        *,
        theme: CLITheme = DEFAULT_CLI_THEME,
        live_factory: Callable[..., Live] = Live,
    ) -> None:
        self.console = console
        self.theme = theme
        self._live_factory = live_factory
        self._live: Live | None = None
        self._pulse: _PulseRenderable | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.console.is_terminal and not self.console.is_jupyter)

    def start(self, label: str, detail: str = "", *, timed: bool) -> bool:
        self.stop()
        if not self.enabled:
            return False
        self._pulse = _PulseRenderable(
            label, detail, timed=timed, theme=self.theme
        )
        self._live = self._live_factory(
            self._pulse,
            console=self.console,
            transient=True,
            auto_refresh=timed,
            refresh_per_second=4,
        )
        self._live.start(refresh=True)
        return True

    def bump(self) -> None:
        if self._pulse is None or self._live is None:
            return
        self._pulse.bump()
        self._live.refresh()

    def stop(self) -> None:
        live, self._live = self._live, None
        self._pulse = None
        if live is not None:
            live.stop()
