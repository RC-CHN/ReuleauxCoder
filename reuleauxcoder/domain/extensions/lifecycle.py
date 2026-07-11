"""Unified runner/session lifecycle dispatch owned by one agent scope."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.hooks import (
    HookContext,
    HookPoint,
    HookRegistry,
    RunnerShutdownContext,
    RunnerStartupContext,
    SessionSaveContext,
    SessionStartContext,
)

NotificationSink = Callable[[str, str, str, dict[str, Any]], None]


@dataclass
class LifecycleCoordinator:
    """Serialize lifecycle transitions through the same guarded pipeline."""

    registry: HookRegistry
    notification_sink: NotificationSink | None = None
    _runner_started: bool = False
    _runner_stopped: bool = False
    _active_session_id: str | None = None
    _saved_session_ids: set[str] = field(default_factory=set)

    def runner_started(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        if self._runner_started:
            return
        self._runner_started = True
        self._dispatch(
            HookPoint.RUNNER_STARTUP,
            RunnerStartupContext(
                hook_point=HookPoint.RUNNER_STARTUP,
                metadata=dict(metadata or {}),
            ),
        )

    def session_started(
        self,
        session_id: str | None,
        *,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._active_session_id = session_id
        values = dict(metadata or {})
        values["reason"] = reason
        self._dispatch(
            HookPoint.SESSION_START,
            SessionStartContext(
                hook_point=HookPoint.SESSION_START,
                session_id=session_id,
                metadata=values,
            ),
        )

    def session_saved(
        self,
        session_id: str,
        *,
        session_data: Mapping[str, Any] | None = None,
    ) -> None:
        self._saved_session_ids.add(session_id)
        self._dispatch(
            HookPoint.SESSION_SAVE,
            SessionSaveContext(
                hook_point=HookPoint.SESSION_SAVE,
                session_id=session_id,
                session_data=dict(session_data or {}),
            ),
        )

    def runner_shutdown(self) -> None:
        if self._runner_stopped:
            return
        self._runner_stopped = True
        self._dispatch(
            HookPoint.RUNNER_SHUTDOWN,
            RunnerShutdownContext(hook_point=HookPoint.RUNNER_SHUTDOWN),
        )

    def _dispatch(self, hook_point: HookPoint, context: HookContext) -> bool:
        decisions = self.registry.run_guards(hook_point, context)
        denied = next((decision for decision in decisions if not decision.allowed), None)
        for decision in decisions:
            if decision.warning:
                self._notify(
                    decision.warning,
                    "lifecycle.guard_warning",
                    "warning",
                    {"hook_point": hook_point.value},
                )
        if denied is not None:
            self._notify(
                denied.reason or f"Lifecycle transition denied: {hook_point.value}",
                "lifecycle.denied",
                "error",
                {"hook_point": hook_point.value},
            )
            return False
        try:
            transformed = self.registry.run_transforms(hook_point, context)
        except Exception:
            # HookRegistry already emitted a structured hook.failure diagnostic.
            # Lifecycle cleanup/save callers must remain deterministic.
            return False
        self.registry.run_observers(hook_point, transformed)
        return True

    def _notify(
        self, message: str, code: str, severity: str, details: dict[str, Any]
    ) -> None:
        if self.notification_sink is not None:
            self.notification_sink(message, code, severity, details)
