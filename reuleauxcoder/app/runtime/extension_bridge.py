"""Bridge legacy HookRegistry lifecycle into the scoped extension runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reuleauxcoder.domain.hooks import (
    HookPoint,
    RunnerShutdownContext,
    RunnerStartupContext,
    SessionStartContext,
)


@dataclass
class LegacyHookLifecycleParticipant:
    """Temporary adapter while individual hooks migrate to extension ports."""

    agent: Any
    ui_bus: Any
    session_id: str | None
    _started: bool = False
    _disposed: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._run(
            HookPoint.RUNNER_STARTUP,
            RunnerStartupContext(
                hook_point=HookPoint.RUNNER_STARTUP,
                metadata={"ui_bus": self.ui_bus},
            ),
        )
        self._run(
            HookPoint.SESSION_START,
            SessionStartContext(
                hook_point=HookPoint.SESSION_START,
                session_id=self.session_id,
                metadata={"ui_bus": self.ui_bus},
            ),
        )

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._run(
            HookPoint.RUNNER_SHUTDOWN,
            RunnerShutdownContext(hook_point=HookPoint.RUNNER_SHUTDOWN),
        )

    def _run(self, hook_point: HookPoint, context) -> None:
        for decision in self.agent.hook_registry.run_guards(hook_point, context):
            if not decision.allowed:
                return
        self.agent.hook_registry.run_transforms(hook_point, context)
        self.agent.hook_registry.run_observers(hook_point, context)
