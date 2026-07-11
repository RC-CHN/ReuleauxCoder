from reuleauxcoder.domain.extensions import LifecycleCoordinator
from reuleauxcoder.domain.hooks import (
    GuardDecision,
    GuardHook,
    HookContext,
    HookPoint,
    HookRegistry,
    ObserverHook,
)


class RecordingObserver(ObserverHook[HookContext]):
    def __init__(self, bucket):
        super().__init__(name="recorder")
        self.bucket = bucket

    def run(self, context) -> None:
        self.bucket.append(
            (
                context.hook_point,
                context.session_id,
                context.metadata.get("reason"),
            )
        )


class WarningGuard(GuardHook[HookContext]):
    def run(self, context: HookContext) -> GuardDecision:
        return GuardDecision.warn("lifecycle warning")


def test_lifecycle_coordinator_routes_all_session_transitions() -> None:
    bucket = []
    registry = HookRegistry()
    observer = RecordingObserver(bucket)
    for point in HookPoint:
        registry.register(point, observer)
    coordinator = LifecycleCoordinator(registry)

    coordinator.runner_started()
    coordinator.runner_started()
    coordinator.session_started("session-1", reason="startup")
    coordinator.session_saved("session-1")
    coordinator.session_started("session-2", reason="new")
    coordinator.session_started("session-1", reason="restore")
    coordinator.runner_shutdown()
    coordinator.runner_shutdown()

    assert bucket == [
        (HookPoint.RUNNER_STARTUP, None, None),
        (HookPoint.SESSION_START, "session-1", "startup"),
        (HookPoint.SESSION_SAVE, "session-1", None),
        (HookPoint.SESSION_START, "session-2", "new"),
        (HookPoint.SESSION_START, "session-1", "restore"),
        (HookPoint.RUNNER_SHUTDOWN, None, None),
    ]


def test_lifecycle_guard_warning_uses_structured_notification_sink() -> None:
    notifications = []
    registry = HookRegistry()
    registry.register(HookPoint.SESSION_SAVE, WarningGuard(name="warning"))
    coordinator = LifecycleCoordinator(
        registry,
        notification_sink=lambda message, code, severity, details: notifications.append(
            (message, code, severity, details)
        ),
    )

    coordinator.session_saved("session-1")

    assert notifications == [
        (
            "lifecycle warning",
            "lifecycle.guard_warning",
            "warning",
            {"hook_point": "session_save"},
        )
    ]
