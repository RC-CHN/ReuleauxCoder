from __future__ import annotations

import threading

from reuleauxcoder.interfaces.cli.output import CLIOutputCoordinator
from reuleauxcoder.interfaces.events import UIEvent


class _Renderer:
    def __init__(self) -> None:
        self.events = []
        self.thread_ids = []
        self.closed = False

    def on_ui_event(self, event) -> None:
        self.events.append(event)
        self.thread_ids.append(threading.get_ident())

    def close(self) -> None:
        self.closed = True


def test_owner_thread_renders_immediately() -> None:
    renderer = _Renderer()
    output = CLIOutputCoordinator(renderer)

    output.on_ui_event(UIEvent.info("foreground"))

    assert [event.message for event in renderer.events] == ["foreground"]


def test_worker_thread_is_queued_until_owner_drains() -> None:
    renderer = _Renderer()
    owner = threading.get_ident()
    output = CLIOutputCoordinator(renderer)

    worker = threading.Thread(
        target=output.on_ui_event, args=(UIEvent.info("background"),)
    )
    worker.start()
    worker.join()

    assert renderer.events == []
    assert output.drain() == 1
    assert [event.message for event in renderer.events] == ["background"]
    assert renderer.thread_ids == [owner]


def test_worker_cannot_drain_cli_output() -> None:
    output = CLIOutputCoordinator(_Renderer())
    errors = []

    def drain() -> None:
        try:
            output.drain()
        except Exception as error:  # noqa: BLE001 - assertion captures contract
            errors.append(error)

    worker = threading.Thread(target=drain)
    worker.start()
    worker.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_close_drains_and_releases_renderer() -> None:
    renderer = _Renderer()
    output = CLIOutputCoordinator(renderer)
    thread = threading.Thread(
        target=output.on_ui_event, args=(UIEvent.info("last"),)
    )
    thread.start()
    thread.join()

    output.close()

    assert [event.message for event in renderer.events] == ["last"]
    assert renderer.closed is True
