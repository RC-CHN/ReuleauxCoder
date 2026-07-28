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
        secret_prompt_fn: Callable[[str], str] | None = None,
    ):
        self.ui_bus = ui_bus
        self._prompt = prompt_fn
        self._secret_prompt = secret_prompt_fn or (
            lambda message: pt_prompt(message, is_password=True)
        )
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
                    answer = (
                        self._secret_prompt(prompt)
                        if request.secret
                        else self._prompt(prompt)
                    )
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
                    session_hint = ", s=session" if request.grant_options else ""
                    answer = self._prompt(
                        f"Select [1/2, y/n{session_hint}, f=feedback]: "
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    self._interrupted()
                    return ReviewResponse(
                        approved=False, cancelled=True, reason="approval interrupted"
                    )

                if answer in {"1", "y", "yes"}:
                    return ReviewResponse(approved=True, action="allow_once")
                if answer in {"2", "n", "no"}:
                    return ReviewResponse(approved=False, action="deny")
                if answer == "s" and request.grant_options:
                    selected = self._choose_review_grant(request)
                    if selected is not None:
                        return ReviewResponse(
                            approved=True,
                            action="allow_session",
                            selected_id=selected,
                        )
                    continue
                if answer == "f":
                    try:
                        feedback = self._prompt(
                            "Feedback for the model (blank returns): "
                        ).strip()
                    except (KeyboardInterrupt, EOFError):
                        self._interrupted()
                        return ReviewResponse(
                            approved=False,
                            cancelled=True,
                            reason="approval interrupted",
                        )
                    if feedback:
                        return ReviewResponse(
                            approved=False,
                            action="deny",
                            reason=feedback,
                        )
                    continue
                self.ui_bus.warning(
                    "Please select one of the advertised actions.",
                    kind=UIEventKind.APPROVAL,
                )

    def _choose_review_grant(self, request: ReviewRequest) -> str | None:
        self.ui_bus.info(
            "\n".join(
                f"[{index}] {option.label} — {option.description}"
                f"{' — broader scope' if option.broad else ''}"
                for index, option in enumerate(request.grant_options, 1)
            ),
            kind=UIEventKind.APPROVAL,
        )
        while True:
            try:
                answer = self._prompt(
                    "Grant scope (blank returns to review): "
                ).strip()
            except (KeyboardInterrupt, EOFError):
                return None
            if not answer:
                return None
            if answer.isdigit():
                index = int(answer)
                if 1 <= index <= len(request.grant_options):
                    return request.grant_options[index - 1].id
            self.ui_bus.warning(
                "Please enter a valid scope number.",
                kind=UIEventKind.APPROVAL,
            )
