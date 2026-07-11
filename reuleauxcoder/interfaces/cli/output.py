"""Thread-safe output scheduling for the CLI adapter."""

from __future__ import annotations

import queue
import threading

from reuleauxcoder.interfaces.events import UIEvent


class CLIOutputCoordinator:
    """Keep Rich rendering on the CLI owner thread.

    Agent events emitted by the foreground chat loop render immediately. Events
    from subagents and other background workers are queued until the REPL reaches
    a safe drain point, so they never write into an active prompt from a worker.
    """

    def __init__(self, renderer, *, owner_thread_id: int | None = None):
        self.renderer = renderer
        self.owner_thread_id = owner_thread_id or threading.get_ident()
        self._pending: queue.Queue[UIEvent] = queue.Queue()
        self._closed = False

    def on_ui_event(self, event: UIEvent) -> None:
        if self._closed:
            return
        if threading.get_ident() == self.owner_thread_id:
            self.renderer.on_ui_event(event)
            return
        self._pending.put(event)

    def drain(self) -> int:
        """Render all queued events from the owner thread."""
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError("CLI output may only be drained by its owner thread")
        drained = 0
        while True:
            try:
                event = self._pending.get_nowait()
            except queue.Empty:
                return drained
            if not self._closed:
                self.renderer.on_ui_event(event)
                drained += 1

    def close(self) -> None:
        if self._closed:
            return
        if threading.get_ident() == self.owner_thread_id:
            self.drain()
        self._closed = True
        self.renderer.close()
