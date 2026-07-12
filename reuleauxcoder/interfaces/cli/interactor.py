"""CLI implementation of the shared UIInteractor protocol."""

from __future__ import annotations

from collections.abc import Callable
import sys
import threading

from prompt_toolkit import prompt as pt_prompt

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


class CLIUIInteractor:
    """Blocking terminal-based UI interaction adapter."""

    def __init__(
        self,
        ui_bus: UIEventBus,
        *,
        prompt_fn: Callable[[str], str] = pt_prompt,
    ):
        self.ui_bus = ui_bus
        self._prompt = prompt_fn
        self._interaction_lock = threading.Lock()

    @staticmethod
    def _finish_interrupted_prompt() -> None:
        """Move structured output off a partially painted input line."""
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _interrupted(self) -> None:
        self._finish_interrupted_prompt()
        self.ui_bus.warning("Interrupted.", kind=UIEventKind.APPROVAL)

    def notify(self, event: UIEvent) -> None:
        """Forward a notification into the UI bus."""
        self.ui_bus.emit(event)

    def confirm(self, request: ConfirmRequest) -> ConfirmResponse:
        with self._interaction_lock:
            self.ui_bus.emit_interaction_prompt(request)
            while True:
                try:
                    answer = self._prompt("Confirm? [y/n]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    self._interrupted()
                    return ConfirmResponse(confirmed=False, cancelled=True)
                if answer in {"y", "yes"}:
                    return ConfirmResponse(confirmed=True)
                if answer in {"n", "no"}:
                    return ConfirmResponse(confirmed=False)
                self.ui_bus.warning(
                    "Please enter 'y' or 'n'.", kind=UIEventKind.COMMAND
                )

    def choose_one(self, request: ChooseOneRequest) -> ChooseOneResponse:
        with self._interaction_lock:
            self.ui_bus.emit_interaction_prompt(request)
            if not request.items:
                self.ui_bus.warning("No options available.", kind=UIEventKind.COMMAND)
                return ChooseOneResponse(selected_id=None, cancelled=True)

            prompt = "Choose one"
            if request.allow_cancel:
                prompt += " (blank to cancel)"
            prompt += ": "

            while True:
                try:
                    answer = self._prompt(prompt).strip()
                except (KeyboardInterrupt, EOFError):
                    self._interrupted()
                    return ChooseOneResponse(selected_id=None, cancelled=True)
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
            self.ui_bus.emit_interaction_prompt(request)
            prompt = request.prompt
            if request.placeholder:
                prompt += f" ({request.placeholder})"
            if request.initial_value:
                prompt += f" [{request.initial_value}]"
            prompt += ": "

            while True:
                try:
                    answer = self._prompt(prompt)
                except (KeyboardInterrupt, EOFError):
                    self._interrupted()
                    return InputTextResponse(value=None, cancelled=True)
                if answer == "" and request.initial_value:
                    answer = request.initial_value
                if answer == "":
                    if request.allow_empty:
                        return InputTextResponse(value="")
                    return InputTextResponse(value=None, cancelled=True)
                return InputTextResponse(value=answer)

    def review(self, request: ReviewRequest) -> ReviewResponse:
        with self._interaction_lock:
            self.ui_bus.emit_interaction_prompt(request)

            while True:
                try:
                    answer = (
                        self._prompt(
                            f"{request.approve_label}/{request.reject_label}? [y/n]: "
                        )
                        .strip()
                        .lower()
                    )
                except (KeyboardInterrupt, EOFError):
                    self._interrupted()
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
