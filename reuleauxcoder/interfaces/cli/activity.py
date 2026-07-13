"""Transient FORGE activity pulse for terminal-only live feedback."""

from __future__ import annotations

from collections.abc import Callable
from collections import deque
import threading
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
        self.tail: tuple[tuple[str, str], ...] = ()

    def bump(self) -> None:
        self.phase = (self.phase + 1) % len(_PHASES)

    def set_tail(self, tail: tuple[tuple[str, str], ...]) -> None:
        self.tail = tail

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,  # noqa: ARG002
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
        for stream, line in self.tail:
            row.append("\n")
            marker = "!" if stream == "stderr" else "›"
            tone = DisplayTone.WARNING if stream == "stderr" else DisplayTone.NEUTRAL
            row.append(f" {marker} ", style=self.theme.style(tone))
            row.append(line, style=self.theme.style(tone))
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
        self._tail: deque[tuple[str, str]] = deque(maxlen=5)
        self._partials: dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return bool(self.console.is_terminal and not self.console.is_jupyter)

    @property
    def is_active(self) -> bool:
        return self._live is not None

    def start(
        self,
        label: str,
        detail: str = "",
        *,
        timed: bool,
        retain: bool = False,
    ) -> bool:
        with self._lock:
            self.stop()
            if not self.enabled:
                return False
            self._tail.clear()
            self._partials.clear()
            self._pulse = _PulseRenderable(label, detail, timed=timed, theme=self.theme)
            self._live = self._live_factory(
                self._pulse,
                console=self.console,
                transient=not retain,
                auto_refresh=timed,
                refresh_per_second=4,
            )
            self._live.start(refresh=True)
            return True

    def bump(self) -> None:
        with self._lock:
            if self._pulse is None or self._live is None:
                return
            self._pulse.bump()
            self._live.refresh()

    def push_output(self, text: str, *, stream: str = "stdout") -> None:
        """Refresh the human-only five-line tail without retaining model text."""
        if not text:
            return
        with self._lock:
            if self._pulse is None or self._live is None:
                return
            pending = self._partials.get(stream, "")
            for part in text.splitlines(keepends=True):
                pending += part
                if part.endswith(("\n", "\r")):
                    self._tail.append((stream, pending.rstrip("\r\n")))
                    pending = ""
            self._partials[stream] = pending
            visible = list(self._tail)
            for pending_stream, value in self._partials.items():
                if value:
                    visible.append((pending_stream, value))
            self._pulse.set_tail(tuple(visible[-5:]))
            self._pulse.bump()
            self._live.refresh()

    def stop(self) -> None:
        with self._lock:
            live, self._live = self._live, None
            self._pulse = None
            self._tail.clear()
            self._partials.clear()
            if live is not None:
                live.stop()
