import threading

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.extensions.remote_exec.http_service import _RemoteChatSession
from reuleauxcoder.extensions.remote_exec.protocol import ChatControlRequest


class _LLM:
    model = "test"


def test_chat_control_admission_is_idempotent_and_cursor_replayable() -> None:
    admitted = []
    session = _RemoteChatSession(chat_id="chat", peer_id="peer", running=True)
    session.bind_chat_control(
        admit_steering=lambda content: admitted.append(content) or "steer-1",
        interrupt_intent=lambda: "promoted",
        stop_turn=lambda: None,
    )
    request = ChatControlRequest(
        peer_token="token",
        chat_id="chat",
        control_id="control-1",
        action="admit_steering",
        content="change direction",
    )

    first = session.apply_control(request)
    second = session.apply_control(request)

    assert first == second
    assert first.outcome == "admitted"
    assert admitted == ["change direction"]
    events, done, cursor = session.wait_events(0, 0)
    assert done is False
    assert cursor == 2
    assert [event["type"] for event in events] == [
        "control_outcome",
        "steering_admitted",
    ]


def test_chat_control_uses_agent_authority_for_promote_then_stop() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._current_turn_id = "turn"
    agent._accepting_user_steering = True
    session = _RemoteChatSession(chat_id="chat", peer_id="peer", running=True)
    session.bind_chat_control(
        admit_steering=agent.admit_user_steering,
        interrupt_intent=agent.request_interrupt_intent,
        stop_turn=agent.request_stop,
    )

    admitted = session.apply_control(
        ChatControlRequest(
            "token", "chat", "admit", "admit_steering", "pivot"
        )
    )
    promoted = session.apply_control(
        ChatControlRequest("token", "chat", "interrupt-1", "interrupt_intent")
    )
    stopped = session.apply_control(
        ChatControlRequest("token", "chat", "interrupt-2", "interrupt_intent")
    )

    assert admitted.outcome == "admitted"
    assert promoted.outcome == "promoted"
    assert stopped.outcome == "stop_requested"
    assert agent.stop_requested()
    assert agent.pending_user_steering() == ()


def test_chat_end_race_rejects_new_text_without_consuming_it() -> None:
    calls = []
    session = _RemoteChatSession(chat_id="chat", peer_id="peer")
    session.bind_chat_control(
        admit_steering=lambda content: calls.append(content) or "steer",
        interrupt_intent=lambda: "promoted",
        stop_turn=lambda: None,
    )
    session.mark_done()

    response = session.apply_control(
        ChatControlRequest(
            "token", "chat", "late", "admit_steering", "preserve me"
        )
    )

    assert response.outcome == "already_done"
    assert calls == []


def test_not_ready_control_can_retry_same_id_after_agent_binding() -> None:
    admitted = []
    session = _RemoteChatSession(chat_id="chat", peer_id="peer", running=True)
    request = ChatControlRequest(
        "token", "chat", "retryable", "admit_steering", "preserve me"
    )

    before_binding = session.apply_control(request)
    session.bind_chat_control(
        admit_steering=lambda content: admitted.append(content) or "steer",
        interrupt_intent=lambda: "promoted",
        stop_turn=lambda: None,
    )
    after_binding = session.apply_control(request)

    assert before_binding.reason == "chat_control_not_ready"
    assert after_binding.outcome == "admitted"
    assert admitted == ["preserve me"]


def test_concurrent_duplicate_control_id_admits_exactly_once() -> None:
    admitted = []
    session = _RemoteChatSession(chat_id="chat", peer_id="peer", running=True)
    session.bind_chat_control(
        admit_steering=lambda content: admitted.append(content) or "steer",
        interrupt_intent=lambda: "promoted",
        stop_turn=lambda: None,
    )
    request = ChatControlRequest(
        "token", "chat", "duplicate", "admit_steering", "once"
    )
    responses = []

    threads = [
        threading.Thread(target=lambda: responses.append(session.apply_control(request)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert admitted == ["once"]
    assert len(responses) == 4
    assert all(response == responses[0] for response in responses)
