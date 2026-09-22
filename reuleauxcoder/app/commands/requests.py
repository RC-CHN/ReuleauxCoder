"""Structured action requests, independent of slash syntax and UI frameworks."""

from dataclasses import dataclass
from typing import Literal

from reuleauxcoder.domain.plan import PlanState, ProgressState


@dataclass(frozen=True, slots=True)
class ActionRequest:
    action_id: str
    command: object


@dataclass(frozen=True, slots=True)
class CommandResult:
    control: Literal["continue", "chat", "exit", "queued"] = "continue"
    session_id: str | None = None
    session_changed: bool = False
    clear_transcript: bool = False
    plan: PlanState | None = None
    progress: ProgressState | None = None
    response: str | None = None
