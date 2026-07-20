from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.services.llm.client import LLMRequestCancelled


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


class _PivotLoop:
    last_response_streamed = False

    def __init__(self, *, raise_cancelled: bool = False) -> None:
        self.agent = None
        self.calls = 0
        self.raise_cancelled = raise_cancelled

    def run(self) -> str:
        self.calls += 1
        if self.calls == 1:
            assert self.agent.submit_user_steering("do this instead")
            self.agent.request_stop()
            if self.raise_cancelled:
                raise LLMRequestCancelled("LLM stream cancelled")
            return "(stopped by cancellation request)"
        assert not self.agent.stop_requested()
        assert self.agent._drain_user_steering() == 1
        return "pivoted answer"


def test_interrupt_with_queued_steering_pivots_instead_of_stopping() -> None:
    loop = _PivotLoop()
    agent = Agent(llm=_LLM(), tools=[], loop=loop)
    loop.agent = agent

    result = agent.chat("initial goal")

    assert result == "pivoted answer"
    assert loop.calls == 2
    assert [message["content"] for message in agent.messages] == [
        "initial goal",
        "do this instead",
    ]
    assert not agent.stop_requested()
    assert agent._pending_user_steering == []


def test_interrupt_with_queued_steering_pivots_past_stream_cancellation() -> None:
    loop = _PivotLoop(raise_cancelled=True)
    agent = Agent(llm=_LLM(), tools=[], loop=loop)
    loop.agent = agent

    result = agent.chat("initial goal")

    assert result == "pivoted answer"
    assert loop.calls == 2
    assert [message["content"] for message in agent.messages] == [
        "initial goal",
        "do this instead",
    ]


class _PlainStopLoop:
    last_response_streamed = False

    def __init__(self) -> None:
        self.agent = None

    def run(self) -> str:
        self.agent.request_stop()
        return "(stopped by cancellation request)"


def test_interrupt_without_queued_steering_stops_cleanly() -> None:
    loop = _PlainStopLoop()
    agent = Agent(llm=_LLM(), tools=[], loop=loop)
    loop.agent = agent

    result = agent.chat("initial goal")

    assert result == "(stopped by cancellation request)"
    assert agent._pending_user_steering == []


def test_drain_emits_user_event_and_exposes_pending_preview() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._current_turn_id = "turn"
    agent._accepting_user_steering = True
    emitted = []
    agent._emit_event = emitted.append

    assert agent.submit_user_steering("first direction")
    assert agent.submit_user_steering("second direction")
    assert agent.pending_user_steering() == ("first direction", "second direction")

    assert agent._drain_user_steering() == 2

    assert agent.pending_user_steering() == ()
    assert [(event.data["code"], event.data["message"]) for event in emitted] == [
        ("user", "first direction"),
        ("user", "second direction"),
    ]


def test_compression_is_skipped_while_stop_is_requested() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.request_stop()

    class _ExplodingContext:
        def maybe_compress(self, *_args, **_kwargs):
            raise AssertionError("compression must not run during unwind")

    agent.context = _ExplodingContext()

    assert agent.maybe_compress_context(agent.llm, reason="test") is False
