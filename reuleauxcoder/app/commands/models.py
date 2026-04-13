"""Shared command/action runtime models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from reuleauxcoder.interfaces.events import UIEvent, UIEventBus
from reuleauxcoder.interfaces.interactions import UIInteractor
from reuleauxcoder.interfaces.ui_registry import UIProfile

if TYPE_CHECKING:
    from reuleauxcoder.app.commands.registry import ActionRegistry
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.skills.service import SkillsService


@dataclass(slots=True)
class OpenViewRequest:
    """Structured request for UI layers to open or focus a view."""

    view_type: str
    title: str
    payload: dict[str, object] = field(default_factory=dict)
    focus: bool = True
    reuse_key: str | None = None


@dataclass(slots=True)
class CommandResult:
    """Common result returned by shared command handlers."""

    action: Literal["continue", "chat", "exit"] = "continue"
    session_id: str | None = None
    session_exit_time: str | None = None
    notifications: list[UIEvent] = field(default_factory=list)
    view_requests: list[OpenViewRequest] = field(default_factory=list)
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class CommandContext:
    """Shared runtime context passed to command handlers."""

    agent: Agent
    config: Config
    ui_bus: UIEventBus
    ui_profile: UIProfile | None = None
    action_registry: ActionRegistry | None = None
    ui_interactor: UIInteractor | None = None
    sessions_dir: Path | None = None
    skills_service: SkillsService | None = None
