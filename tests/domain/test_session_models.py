from reuleauxcoder.domain.session.models import SessionRuntimeState


def test_session_runtime_state_round_trips_plan_and_progress() -> None:
    state = SessionRuntimeState(
        plan_state={
            "revision": 2,
            "items": [
                {"step": "Verify", "active_form": "Verifying", "status": "in_progress"}
            ],
        },
        progress_state={
            "phase": "verifying",
            "summary": "Running tests",
            "revision": 3,
        },
    )

    restored = SessionRuntimeState.from_dict(state.to_dict())

    assert restored.plan_state == state.plan_state
    assert restored.progress_state == state.progress_state
