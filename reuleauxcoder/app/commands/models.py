"""Shared command/action runtime models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from reuleauxcoder.interfaces.interactions import UIInteractor
from reuleauxcoder.interfaces.events import ReasoningNoticePayload, UIEventPayload
from reuleauxcoder.interfaces.ui_registry import UIProfile
from reuleauxcoder.app.commands.view_models import ViewModel

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
    view_model: ViewModel
    focus: bool = True
    reuse_key: str | None = None
    action: Literal["open", "refresh"] = "open"


@dataclass(frozen=True, slots=True)
class NotificationEffect:
    """Framework-neutral notification requested by a command."""

    message: str
    level: Literal["info", "success", "warning", "error", "debug"] = "info"
    kind: str = "command"
    metadata: Mapping[str, object] = field(default_factory=dict)
    payload: UIEventPayload | None = None


@dataclass(frozen=True, slots=True)
class StateChangeEffect:
    """One typed key/value runtime state observation returned by a command."""

    key: str
    value: object


@dataclass(slots=True)
class CommandEffect:
    """The only externally visible result returned by a command use case."""

    control: Literal["continue", "chat", "exit"] = "continue"
    session_id: str | None = None
    session_exit_time: str | None = None
    notifications: list[NotificationEffect] = field(default_factory=list)
    views: list[OpenViewRequest] = field(default_factory=list)
    interactions: list[object] = field(default_factory=list)
    state_changes: list[StateChangeEffect] = field(default_factory=list)

    @property
    def state(self) -> dict[str, object]:
        """Convenient read-only-by-convention projection of typed state changes."""
        return {change.key: change.value for change in self.state_changes}

    @staticmethod
    def _kind_value(kind: object) -> str:
        return str(getattr(kind, "value", kind or "command"))

    def _notify(
        self,
        level: str,
        message: str,
        *,
        kind=None,
        payload: UIEventPayload | None = None,
        **metadata: object,
    ) -> None:
        self.notifications.append(
            NotificationEffect(
                message=message,
                level=level,  # type: ignore[arg-type]
                kind=self._kind_value(kind),
                metadata=dict(metadata),
                payload=payload,
            )
        )

    def info(self, message: str, *, kind=None, **metadata: object) -> None:
        self._notify("info", message, kind=kind, **metadata)

    def success(self, message: str, *, kind=None, **metadata: object) -> None:
        self._notify("success", message, kind=kind, **metadata)

    def warning(self, message: str, *, kind=None, **metadata: object) -> None:
        self._notify("warning", message, kind=kind, **metadata)

    def error(self, message: str, *, kind=None, **metadata: object) -> None:
        self._notify("error", message, kind=kind, **metadata)

    def debug(self, message: str, *, kind=None, **metadata: object) -> None:
        self._notify("debug", message, kind=kind, **metadata)

    def reasoning(self, content: str, *, title: str = "Reasoning", kind=None) -> None:
        self._notify(
            "info",
            content,
            kind=kind,
            payload=ReasoningNoticePayload(title=title),
        )

    def open_view(
        self,
        view_type: str,
        *,
        title: str,
        view_model: ViewModel,
        focus: bool = True,
        reuse_key: str | None = None,
    ) -> None:
        if view_model.view_type != view_type:
            raise ValueError("view_type must match view_model.view_type")
        self.views.append(
            OpenViewRequest(
                view_type=view_type,
                title=title,
                view_model=view_model,
                focus=focus,
                reuse_key=reuse_key,
                action="open",
            )
        )

    def refresh_view(
        self,
        view_type: str,
        *,
        title: str | None = None,
        view_model: ViewModel,
        reuse_key: str | None = None,
    ) -> None:
        if view_model.view_type != view_type:
            raise ValueError("view_type must match view_model.view_type")
        self.views.append(
            OpenViewRequest(
                view_type=view_type,
                title=title or view_type,
                view_model=view_model,
                focus=False,
                reuse_key=reuse_key,
                action="refresh",
            )
        )

    def finish(
        self,
        *,
        control: Literal["continue", "chat", "exit"] = "continue",
        session_id: str | None = None,
        session_exit_time: str | None = None,
        state_changes: Mapping[str, object] | None = None,
    ) -> "CommandEffect":
        """Finalize and return this same effect instance."""
        self.control = control
        self.session_id = session_id
        self.session_exit_time = session_exit_time
        if state_changes:
            self.state_changes.extend(
                StateChangeEffect(key=key, value=value)
                for key, value in state_changes.items()
            )
        return self


@dataclass(slots=True)
class CommandContext:
    """Shared runtime context passed to command handlers."""

    agent: Agent
    config: Config
    effect: CommandEffect
    ui_profile: UIProfile | None = None
    action_registry: ActionRegistry | None = None
    ui_interactor: UIInteractor | None = None
    sessions_dir: Path | None = None
    skills_service: SkillsService | None = None
