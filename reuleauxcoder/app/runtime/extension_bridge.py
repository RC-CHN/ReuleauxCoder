"""Bridge legacy HookRegistry lifecycle into the scoped extension runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from reuleauxcoder.domain.extensions import LifecycleCoordinator


@dataclass
class LegacyHookLifecycleParticipant:
    """Temporary adapter while individual hooks migrate to extension ports."""

    coordinator: LifecycleCoordinator
    ui_bus: Any
    session_id: str | None
    _started: bool = False
    _disposed: bool = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.coordinator.runner_started(metadata={"ui_bus": self.ui_bus})
        self.coordinator.session_started(
            self.session_id,
            reason="startup",
            metadata={"ui_bus": self.ui_bus},
        )

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.coordinator.runner_shutdown()
