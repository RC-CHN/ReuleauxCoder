"""Shared command/action runtime models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
    action: Literal["open", "refresh"] = "open"


@dataclass(frozen=True, slots=True)
class NotificationEffect:
    """Framework-neutral notification requested by a command."""

    message: str
    level: Literal["info", "success", "warning", "error", "debug"] = "info"
    kind: str = "command"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandEffect:
    """The only externally visible result returned by a command use case."""

    action: Literal["continue", "chat", "exit"] = "continue"
    session_id: str | None = None
    session_exit_time: str | None = None
    notifications: list[NotificationEffect] = field(default_factory=list)
    view_requests: list[OpenViewRequest] = field(default_factory=list)
    payload: dict[str, object] = field(default_factory=dict)


# Compatibility name while command modules migrate their imports.
CommandResult = CommandEffect


class CommandEffectBuilder:
    """Imperative compatibility builder used inside existing command handlers.

    It deliberately has the small surface of ``UIEventBus`` that handlers used,
    but records typed effects and never publishes to a UI.
    """

    def __init__(self) -> None:
        self.notifications: list[NotificationEffect] = []
        self.view_requests: list[OpenViewRequest] = []

    @staticmethod
    def _kind_value(kind: object) -> str:
        return str(getattr(kind, "value", kind or "command"))

    def _notify(self, level: str, message: str, *, kind=None, **data: Any) -> None:
        self.notifications.append(
            NotificationEffect(
                message=message,
                level=level,  # type: ignore[arg-type]
                kind=self._kind_value(kind),
                data=dict(data),
            )
        )

    def info(self, message: str, *, kind=None, **data: Any) -> None:
        self._notify("info", message, kind=kind, **data)

    def success(self, message: str, *, kind=None, **data: Any) -> None:
        self._notify("success", message, kind=kind, **data)

    def warning(self, message: str, *, kind=None, **data: Any) -> None:
        self._notify("warning", message, kind=kind, **data)

    def error(self, message: str, *, kind=None, **data: Any) -> None:
        self._notify("error", message, kind=kind, **data)

    def debug(self, message: str, *, kind=None, **data: Any) -> None:
        self._notify("debug", message, kind=kind, **data)

    def open_view(
        self,
        view_type: str,
        *,
        title: str,
        payload: dict[str, Any] | None = None,
        focus: bool = True,
        reuse_key: str | None = None,
    ) -> None:
        self.view_requests.append(
            OpenViewRequest(
                view_type=view_type,
                title=title,
                payload=payload or {},
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
        payload: dict[str, Any] | None = None,
        reuse_key: str | None = None,
    ) -> None:
        self.view_requests.append(
            OpenViewRequest(
                view_type=view_type,
                title=title or view_type,
                payload=payload or {},
                focus=False,
                reuse_key=reuse_key,
                action="refresh",
            )
        )

    def build(self, base: CommandEffect) -> CommandEffect:
        views = list(self.view_requests)
        seen = {
            (view.action, view.view_type, view.reuse_key, view.title) for view in views
        }
        for view in base.view_requests:
            key = (view.action, view.view_type, view.reuse_key, view.title)
            if key not in seen:
                views.append(view)
                seen.add(key)
        return CommandEffect(
            action=base.action,
            session_id=base.session_id,
            session_exit_time=base.session_exit_time,
            notifications=[*self.notifications, *base.notifications],
            view_requests=views,
            payload=dict(base.payload),
        )


@dataclass(slots=True)
class CommandContext:
    """Shared runtime context passed to command handlers."""

    agent: Agent
    config: Config
    ui_bus: CommandEffectBuilder
    ui_profile: UIProfile | None = None
    action_registry: ActionRegistry | None = None
    ui_interactor: UIInteractor | None = None
    sessions_dir: Path | None = None
    skills_service: SkillsService | None = None
