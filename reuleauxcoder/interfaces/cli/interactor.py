"""CLI implementation of the shared UIInteractor protocol."""

from __future__ import annotations

import threading

from reuleauxcoder.interfaces.events import UIEvent, UIEventBus, UIEventKind
from reuleauxcoder.interfaces.interactions import (
    ChooseOneRequest,
    ChooseOneResponse,
    ConfirmRequest,
    ConfirmResponse,
    InputTextRequest,
    InputTextResponse,
    ReviewRequest,
    ReviewResponse,
)
from reuleauxcoder.interfaces.cli.render import render_diff_panel


class CLIUIInteractor:
    """Blocking terminal-based UI interaction adapter."""

    def __init__(self, ui_bus: UIEventBus):
        self.ui_bus = ui_bus
        self._interaction_lock = threading.Lock()

    def notify(self, event: UIEvent) -> None:
        """Forward a notification into the UI bus."""
        self.ui_bus.emit(event)

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        with self._interaction_lock:
            self.ui_bus.warning(
                request.title, kind=UIEventKind.COMMAND, **request.details
            )
            self.ui_bus.info(request.message, kind=UIEventKind.COMMAND)
            while True:
                answer = input("Confirm? [y/n]: ").strip().lower()
                if answer in {"y", "yes"}:
                    return ConfirmResponse(confirmed=True)
                if answer in {"n", "no"}:
                    return ConfirmResponse(confirmed=False)
                self.ui_bus.warning(
                    "Please enter 'y' or 'n'.", kind=UIEventKind.COMMAND
                )

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        with self._interaction_lock:
            self.ui_bus.info(request.title, kind=UIEventKind.COMMAND, **request.details)
            if request.message:
                self.ui_bus.info(request.message, kind=UIEventKind.COMMAND)
            if not request.items:
                self.ui_bus.warning("No options available.", kind=UIEventKind.COMMAND)
                return ChooseOneResponse(selected_id=None, cancelled=True)

            for index, item in enumerate(request.items, 1):
                suffix = f" — {item.description}" if item.description else ""
                self.ui_bus.info(
                    f"  {index}. {item.label}{suffix}",
                    kind=UIEventKind.COMMAND,
                )

            prompt = "Choose one"
            if request.allow_cancel:
                prompt += " (blank to cancel)"
            prompt += ": "

            while True:
                answer = input(prompt).strip()
                if answer == "" and request.allow_cancel:
                    return ChooseOneResponse(selected_id=None, cancelled=True)
                if answer.isdigit():
                    idx = int(answer)
                    if 1 <= idx <= len(request.items):
                        return ChooseOneResponse(selected_id=request.items[idx - 1].id)
                self.ui_bus.warning(
                    "Please enter a valid number.", kind=UIEventKind.COMMAND
                )

    def input_text(self, request: InputTextRequest) -> InputTextResponse:
        with self._interaction_lock:
            self.ui_bus.info(request.title, kind=UIEventKind.COMMAND, **request.details)
            prompt = request.prompt
            if request.placeholder:
                prompt += f" ({request.placeholder})"
            if request.initial_value:
                prompt += f" [{request.initial_value}]"
            prompt += ": "

            while True:
                answer = input(prompt)
                if answer == "" and request.initial_value:
                    answer = request.initial_value
                if answer == "":
                    if request.allow_empty:
                        return InputTextResponse(value="")
                    return InputTextResponse(value=None, cancelled=True)
                return InputTextResponse(value=answer)

    def review(self, request: ReviewRequest) -> ReviewResponse:
        with self._interaction_lock:
            self.ui_bus.warning(
                request.title, kind=UIEventKind.APPROVAL, **request.metadata
            )
            self.ui_bus.info(request.summary, kind=UIEventKind.APPROVAL)

            for section in request.sections:
                title = section.get("title", "Section")
                kind = section.get("kind", "text")
                content = section.get("content")
                self.ui_bus.info(title, kind=UIEventKind.APPROVAL)
                if kind == "diff" and isinstance(content, str):
                    render_diff_panel(content)
                elif content is not None:
                    self.ui_bus.info(str(content), kind=UIEventKind.APPROVAL)

            while True:
                try:
                    answer = (
                        input(
                            f"{request.approve_label}/{request.reject_label}? [y/n]: "
                        )
                        .strip()
                        .lower()
                    )
                except (KeyboardInterrupt, EOFError):
                    self.ui_bus.warning("Interrupted.", kind=UIEventKind.APPROVAL)
                    return ReviewResponse(
                        approved=False, cancelled=True, reason="approval interrupted"
                    )

                if answer in {"y", "yes"}:
                    return ReviewResponse(approved=True)
                if answer in {"n", "no"}:
                    return ReviewResponse(approved=False)
                self.ui_bus.warning(
                    "Please enter 'y' or 'n'.", kind=UIEventKind.APPROVAL
                )
