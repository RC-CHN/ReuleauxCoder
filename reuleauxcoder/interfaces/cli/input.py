"""Line editing with foreground RPC interaction and output handoff."""

import asyncio
from enum import Enum, auto
from time import monotonic
import re

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app_session, run_in_terminal
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from reuleauxcoder.interfaces.cli.images import ImagePaste, pasted_image_path

from reuleauxcoder.interfaces.cli.prompt import (
    FORGE_USER_PROMPT_STYLE,
    forge_active_prompt,
)


class PromptAction(Enum):
    INTERACTION = auto()
    DETAILS = auto()
    TOOLS = auto()
    INTERRUPT = auto()
    EXIT = auto()


class CLIInput:
    """One native terminal prompt; never creates an alternate screen."""

    def __init__(self, runtime, output, exited, *, history, completer):
        self.runtime = runtime
        self.output = output
        self.exited = exited
        self.draft = Document()
        self._exit_pressed_at = 0.0
        self._size = None
        self.image_labels = lambda: ()
        bindings = KeyBindings()

        @bindings.add(Keys.BracketedPaste)
        def paste(event):
            path = pasted_image_path(event.data)
            if path is None:
                event.current_buffer.insert_text(event.data)
            else:
                self._leave(event.app, ImagePaste(path, event.data))

        @bindings.add("backspace")
        def backspace(event):
            marker = re.search(
                r"\[Image #[1-9]\d*\]$",
                event.current_buffer.document.text_before_cursor,
            )
            count = len(marker[0]) if marker and marker[0] in self.image_labels() else 1
            event.current_buffer.delete_before_cursor(count)

        @bindings.add("f2")
        @bindings.add("c-o")
        def details(event):
            self._leave(event.app, PromptAction.DETAILS)

        @bindings.add("f4")
        def tools(event):
            self._leave(event.app, PromptAction.TOOLS)

        @bindings.add("escape", "enter")
        def newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add("c-c")
        def interrupt(event):
            if event.current_buffer.text:
                event.current_buffer.reset()
            elif runtime.state.running:
                self._leave(event.app, PromptAction.INTERRUPT)
            elif monotonic() - self._exit_pressed_at < 2:
                self._leave(event.app, PromptAction.EXIT)
            else:
                self._exit_pressed_at = monotonic()
                event.app.invalidate()

        self.session = PromptSession(
            forge_active_prompt,
            output=get_app_session().output,
            history=history,
            completer=completer,
            complete_while_typing=False,
            style=FORGE_USER_PROMPT_STYLE,
            key_bindings=bindings,
            bottom_toolbar=self._status,
        )

    def _status(self):
        if monotonic() - self._exit_pressed_at < 2:
            return "Press Ctrl+C again to save and exit"
        state = self.runtime.state
        status = (
            "stopping"
            if state.stopping
            else "steering pending"
            if state.interrupt_pending
            else (self.output.renderer.current_activity or "running")
            if state.running
            else "ready"
        )
        status = status.replace("\n", " ")[:32]
        return (
            f" {status} · queued {len(state.queued_commands)}/{len(state.queued_steering)}"
            " · F2 status · F4 arguments + full tool output"
        )

    def _leave(self, app, action):
        self.draft = app.current_buffer.document
        app.exit(result=action)

    async def _pump(self):
        app = self.session.app
        last_status = self._status()
        try:
            while not app.is_done:
                size = app.output.get_size()
                if size != self._size:
                    self.runtime.resize(size.rows, size.columns)
                    self._size = size
                if self.runtime.peer.closed.is_set():
                    app.exit(exception=ConnectionError("Backend disconnected"))
                    return
                if self.exited.is_set():
                    self._leave(app, PromptAction.EXIT)
                    return
                if self.runtime.has_pending_interactions:
                    self._leave(app, PromptAction.INTERACTION)
                    return
                if self.output.has_pending:
                    rendered = self.output.capture_pending()
                    if rendered:
                        await run_in_terminal(
                            lambda: print(
                                rendered,
                                end="",
                                file=self.output.renderer.console.file,
                                flush=True,
                            )
                        )
                status = self._status()
                if status != last_status:
                    app.invalidate()
                    last_status = status
                await asyncio.sleep(0.05)
        except Exception as error:
            app.exit(exception=error)

    def read(self):
        result = self.session.prompt(
            default=self.draft,
            pre_run=lambda: self.session.app.create_background_task(self._pump()),
        )
        if isinstance(result, str):
            self.draft = Document()
        return result
