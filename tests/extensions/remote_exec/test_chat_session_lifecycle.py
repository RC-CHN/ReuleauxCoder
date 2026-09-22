from reuleauxcoder.extensions.remote_exec.http_service import _RemoteChatSession


def test_cancellation_survives_late_interaction_and_control_registration():
    session = _RemoteChatSession("chat", "peer")
    assert session.request_cancel("host shutdown")
    session.register_interaction("late-approval")
    assert session.wait_interaction("late-approval", timeout_sec=0.1) == (
        None,
        True,
        "chat cancelled",
    )
    stopped = []
    session.bind_chat_control(
        admit_steering=lambda text: None,
        interrupt_intent=lambda: None,
        stop_turn=lambda: stopped.append(True),
    )
    assert stopped == [True]
