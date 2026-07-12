from io import StringIO

from rich.console import Console

from reuleauxcoder.interfaces.cli.activity import CLIActivityPresenter


class _FakeLive:
    def __init__(self, renderable, **kwargs) -> None:
        self.renderable = renderable
        self.kwargs = kwargs
        self.started = False
        self.refreshes = 0
        self.stopped = False

    def start(self, *, refresh: bool) -> None:
        self.started = refresh

    def refresh(self) -> None:
        self.refreshes += 1

    def stop(self) -> None:
        self.stopped = True


def test_activity_is_disabled_for_non_terminal_sinks() -> None:
    console = Console(file=StringIO(), force_terminal=False)
    created = []
    activity = CLIActivityPresenter(
        console, live_factory=lambda *args, **kwargs: created.append((args, kwargs))
    )

    assert activity.start("TOOL", "shell", timed=True) is False
    assert created == []


def test_reasoning_activity_advances_only_when_bumped() -> None:
    stream = StringIO()
    console = Console(
        file=stream,
        record=True,
        force_terminal=True,
        color_system=None,
    )
    lives = []

    def factory(renderable, **kwargs):
        live = _FakeLive(renderable, **kwargs)
        lives.append(live)
        return live

    activity = CLIActivityPresenter(console, live_factory=factory)

    assert activity.start("THINK", "processing", timed=False, retain=True) is True
    activity.bump()
    console.print(lives[0].renderable)
    activity.stop()
    output = console.export_text()

    assert lives[0].kwargs["auto_refresh"] is False
    assert lives[0].kwargs["transient"] is False
    assert lives[0].refreshes == 1
    assert lives[0].stopped is True
    assert "THINK" in output
    assert "··" in output


def test_tool_activity_uses_low_frequency_timed_refresh() -> None:
    console = Console(file=StringIO(), force_terminal=True, color_system=None)
    lives = []

    def factory(renderable, **kwargs):
        live = _FakeLive(renderable, **kwargs)
        lives.append(live)
        return live

    activity = CLIActivityPresenter(console, live_factory=factory)

    activity.start("TOOL", "shell", timed=True)
    activity.stop()

    assert lives[0].kwargs["auto_refresh"] is True
    assert lives[0].kwargs["refresh_per_second"] == 4
    assert lives[0].kwargs["transient"] is True
