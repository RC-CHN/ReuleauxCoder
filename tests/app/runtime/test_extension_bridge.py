from reuleauxcoder.app.runtime.extension_bridge import LegacyHookLifecycleParticipant
from reuleauxcoder.domain.extensions import LifecycleCoordinator
from reuleauxcoder.domain.hooks import HookPoint


class _Registry:
    def __init__(self) -> None:
        self.calls = []

    def run_guards(self, point, context):
        self.calls.append(("guard", point, context.session_id))
        return []

    def run_transforms(self, point, context):
        self.calls.append(("transform", point, context.session_id))
        return context

    def run_observers(self, point, context):
        self.calls.append(("observer", point, context.session_id))


def test_legacy_hook_lifecycle_is_exactly_once() -> None:
    registry = _Registry()
    participant = LegacyHookLifecycleParticipant(
        coordinator=LifecycleCoordinator(registry),
        ui_bus=object(),
        session_id="session-1",
    )

    participant.start()
    participant.start()
    participant.dispose()
    participant.dispose()

    points = [point for phase, point, session_id in registry.calls if phase == "guard"]
    assert points == [
        HookPoint.RUNNER_STARTUP,
        HookPoint.SESSION_START,
        HookPoint.RUNNER_SHUTDOWN,
    ]
