"""Inject a compact process-session inventory into the request-time tail."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.domain.process_manager import ProcessManager

from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.runtime_overlay import (
    has_runtime_overlay_tail,
    inject_runtime_overlay_region,
)
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext


_MAX_INVENTORY_ITEMS = 16
_MAX_COMMAND_CHARS = 160


@dataclass(slots=True)
class ProcessSessionInjectorHook(TransformHook[BeforeLLMRequestContext]):
    """Project live/unobserved process facts without changing stable history."""

    process_manager: ProcessManager | None = field(default=None)

    def __init__(
        self,
        *,
        process_manager: ProcessManager | None = None,
        priority: int = 80,
    ) -> None:
        TransformHook.__init__(
            self,
            name="process_session_injector",
            priority=priority,
            extension_name="core",
        )
        self.process_manager = process_manager

    @classmethod
    def create_from_config(cls, config: "Config") -> "ProcessSessionInjectorHook":
        del config
        return cls(priority=80)

    def bind_runtime_service(self, name: str, service: object | None) -> None:
        if name == "process_manager":
            self.process_manager = service  # type: ignore[assignment]

    def clone_for_scope(self, scope: str) -> "ProcessSessionInjectorHook":
        del scope
        return ProcessSessionInjectorHook(
            process_manager=self.process_manager,
            priority=self.priority,
        )

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        manager = self.process_manager
        if (
            manager is None
            or context.agent_id is None
            or context.session_generation is None
            or not has_runtime_overlay_tail(context.messages)
        ):
            return context
        try:
            sessions = manager.list(
                agent_id=context.agent_id,
                owner_session_id=context.session_id,
                session_generation=context.session_generation,
            )
        except Exception:
            return context
        if not sessions:
            return context

        lines = ['<active_shell_sessions trust="runtime_state">']
        for session in sessions[:_MAX_INVENTORY_ITEMS]:
            command = " ".join(session.command.split())
            if len(command) > _MAX_COMMAND_CHARS:
                command = command[: _MAX_COMMAND_CHARS - 1] + "…"
            lines.extend(
                (
                    "  <session"
                    f' id="{escape(session.session_id, quote=True)}"'
                    f' state="{escape(session.state.value, quote=True)}"'
                    f' tty="{"true" if session.stream_mode == "pty" else "false"}"'
                    f' elapsed_seconds="{session.elapsed_seconds:.1f}"'
                    ">",
                    '    <command trust="untrusted_data">'
                    f"{escape(command, quote=False)}</command>",
                    "  </session>",
                )
            )
        if len(sessions) > _MAX_INVENTORY_ITEMS:
            lines.append(
                f'  <omitted count="{len(sessions) - _MAX_INVENTORY_ITEMS}" />'
            )
        lines.append("</active_shell_sessions>")
        region = "[SHELL SESSIONS]\n" + "\n".join(lines) + "\n"
        inject_runtime_overlay_region(context.messages, region)
        return context
