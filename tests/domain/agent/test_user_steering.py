from reuleauxcoder.domain.agent.agent import Agent


class _LLM:
    model = "test-model"


class _SteeringLoop:
    last_response_streamed = False

    def __init__(self) -> None:
        self.agent = None
        self.calls = 0

    def run(self) -> str:
        self.calls += 1
        if self.calls == 1:
            assert self.agent.submit_user_steering("focus on persistence")
            return "first answer"
        assert self.agent._drain_user_steering() == 1
        return "revised answer"


def test_steering_is_ledger_first_then_projected_at_safe_boundary() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._current_turn_id = "turn"
    agent._accepting_user_steering = True

    assert agent.submit_user_steering("change direction")
    assert agent.messages == []
    event = next(
        event
        for event in agent.history_ledger.events
        if event.kind == "message_committed"
    )
    assert event.kind == "message_committed"
    assert event.payload["source"] == "user_steering"
    assert event.turn_id == "turn"

    assert agent._drain_user_steering() == 1
    assert agent.messages == [{"role": "user", "content": "change direction"}]
    assert [event.kind for event in agent.history_ledger.events] == [
        "message_committed",
        "user_message",
    ]


def test_chat_continues_same_turn_when_steering_wins_completion_race() -> None:
    loop = _SteeringLoop()
    agent = Agent(llm=_LLM(), tools=[], loop=loop)
    loop.agent = agent

    result = agent.chat("initial goal")

    assert result == "revised answer"
    assert loop.calls == 2
    assert [message["content"] for message in agent.messages] == [
        "initial goal",
        "focus on persistence",
    ]
    assert agent._current_turn_id is None
    assert not agent._accepting_user_steering


def test_steering_is_rejected_outside_an_active_turn() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    assert not agent.submit_user_steering("too late")
    assert agent.history_ledger.events == ()
