from reuleauxcoder.domain.agent.agent import Agent, InterruptIntentOutcome
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.llm.models import LLMResponse
from reuleauxcoder.services.llm.client import LLMRequestCancelled
from types import SimpleNamespace


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


def test_steering_is_admitted_then_committed_at_safe_boundary() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._current_turn_id = "turn"
    agent._accepting_user_steering = True

    assert agent.submit_user_steering("change direction")
    assert agent.messages == []
    admitted = agent.history_ledger.events[-1]
    assert admitted.kind == "steering_admitted"
    assert admitted.payload["content"] == "change direction"
    assert admitted.turn_id == "turn"

    assert agent._drain_user_steering(attempt_id="turn:0:1") == 1
    assert agent.messages == [{"role": "user", "content": "change direction"}]
    assert [event.kind for event in agent.history_ledger.events] == [
        "steering_admitted",
        "steering_applied",
        "message_committed",
        "user_message",
    ]
    committed = agent.history_ledger.events[2]
    assert committed.payload["steering_id"] == admitted.payload["steering_id"]
    assert committed.payload["attempt_id"] == "turn:0:1"


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


def test_interrupt_with_queued_steering_stops_and_discards_steering() -> None:
    loop = _PivotLoop()
    agent = Agent(llm=_LLM(), tools=[], loop=loop)
    loop.agent = agent

    result = agent.chat("initial goal")

    assert result == "(stopped by cancellation request)"
    assert loop.calls == 1
    assert [message["content"] for message in agent.messages][0] == "initial goal"
    assert "<turn_interrupted>" in agent.messages[-1]["content"]
    assert agent.stop_requested()
    assert agent._pending_user_steering == []


def test_stream_cancellation_discards_queued_steering() -> None:
    loop = _PivotLoop(raise_cancelled=True)
    agent = Agent(llm=_LLM(), tools=[], loop=loop)
    loop.agent = agent

    result = agent.chat("initial goal")

    assert result == "(stopped by cancellation request)"
    assert loop.calls == 1
    assert [message["content"] for message in agent.messages][0] == "initial goal"
    assert "<turn_interrupted>" in agent.messages[-1]["content"]
    assert agent._pending_user_steering == []


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
    assert [
        (event.event_type, event.data["user_input"]) for event in emitted
    ] == [
        (AgentEventType.USER_STEERING, "first direction"),
        (AgentEventType.USER_STEERING, "second direction"),
    ]


def test_compression_is_skipped_while_stop_is_requested() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.request_stop()

    class _ExplodingContext:
        def maybe_compress(self, *_args, **_kwargs):
            raise AssertionError("compression must not run during unwind")

    agent.context = _ExplodingContext()

    assert agent.maybe_compress_context(agent.llm, reason="test") is False


def test_interrupt_intent_promotes_then_second_gesture_stops_and_discards() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._current_turn_id = "turn"
    agent._accepting_user_steering = True
    assert agent.submit_user_steering("pivot")

    promoted = agent.request_interrupt_intent()
    assert promoted.outcome is InterruptIntentOutcome.PROMOTED
    assert promoted.epoch == 1
    assert agent.round_interrupt_pending()
    assert not agent.stop_requested()

    stopped = agent.request_interrupt_intent()
    assert stopped.outcome is InterruptIntentOutcome.STOP_REQUESTED
    assert stopped.discarded_count == 1
    assert agent.stop_requested()
    assert not agent.round_interrupt_pending()
    assert [event.kind for event in agent.history_ledger.events] == [
        "steering_admitted",
        "steering_discarded",
    ]


class _ImmediateSteeringLLM:
    model = "test-model"
    max_tokens = 128

    def __init__(self) -> None:
        self.agent = None
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            kwargs["on_token"]("partial answer")
            assert self.agent.submit_user_steering("use the newer direction")
            result = self.agent.request_interrupt_intent()
            assert result.outcome is InterruptIntentOutcome.PROMOTED
            assert kwargs["cancellation_event"].is_set()
            raise LLMRequestCancelled("cancel first attempt")
        return LLMResponse(content="revised")


def test_immediate_steering_retries_same_round_with_marker_before_steering() -> None:
    llm = _ImmediateSteeringLLM()
    agent = Agent(llm=llm, tools=[])
    llm.agent = agent
    emitted = []
    agent.add_event_handler(emitted.append)

    assert agent.chat("initial") == "revised"
    assert len(llm.calls) == 2
    assert agent.state.current_round == 0
    assert agent.state.total_model_calls == 2

    second_messages = llm.calls[1]["messages"]
    marker_index = next(
        index
        for index, message in enumerate(second_messages)
        if "<request_interrupted>" in str(message.get("content"))
    )
    steering_index = next(
        index
        for index, message in enumerate(second_messages)
        if message.get("content") == "use the newer direction"
    )
    assert marker_index < steering_index
    assert (
        sum(
            "<request_interrupted>" in str(message.get("content"))
            for message in agent.messages
        )
        == 1
    )
    attempts = [
        event
        for event in agent.history_ledger.events
        if event.kind.startswith("request_attempt_")
    ]
    assert [event.kind for event in attempts] == [
        "request_attempt_dispatched",
        "request_attempt_cancelled",
        "request_attempt_dispatched",
    ]
    assert attempts[0].payload["attempt_id"].endswith(":0:1")
    assert attempts[2].payload["attempt_id"].endswith(":0:2")
    assert any(
        event.event_type is AgentEventType.ASSISTANT_STREAM_INTERRUPTED
        for event in emitted
    )


def test_restore_discards_uncommitted_admission_but_not_committed_steering() -> None:
    source = Agent(llm=_LLM(), tools=[])
    source._current_turn_id = "turn"
    source._accepting_user_steering = True
    assert source.submit_user_steering("lost direction")
    orphan_events = source.history_ledger.events

    restored = Agent(llm=_LLM(), tools=[])
    restored.restore_history_runtime(
        SimpleNamespace(
            id="session",
            history_events=orphan_events,
            replay_envelope=None,
            request_envelopes=(),
            history_completeness="complete",
            checkpoints=(),
            messages=[],
        )
    )

    assert restored.take_recovered_steering_discard_count() == 1
    assert restored.messages == []
    assert restored.history_ledger.events[-1].kind == "steering_discarded"
    assert (
        restored.history_ledger.events[-1].payload["reason"]
        == "session_recovery"
    )

    committed_source = Agent(llm=_LLM(), tools=[])
    committed_source._current_turn_id = "turn"
    committed_source._accepting_user_steering = True
    assert committed_source.submit_user_steering("delivered direction")
    assert committed_source._drain_user_steering(attempt_id="turn:0:1") == 1
    committed = Agent(llm=_LLM(), tools=[])
    committed.restore_history_runtime(
        SimpleNamespace(
            id="session",
            history_events=committed_source.history_ledger.events,
            replay_envelope=None,
            request_envelopes=(),
            history_completeness="complete",
            checkpoints=(),
            messages=committed_source.messages,
        )
    )
    assert committed.take_recovered_steering_discard_count() == 0
    assert committed.messages[-1]["content"] == "delivered direction"


def test_restore_treats_applied_without_message_commit_as_unsent() -> None:
    source = Agent(llm=_LLM(), tools=[])
    source._current_turn_id = "turn"
    source._accepting_user_steering = True
    steering_id = source.admit_user_steering("torn direction")
    assert steering_id is not None
    source.history_ledger.append(
        "steering_applied",
        {"steering_id": steering_id, "attempt_id": "turn:0:1"},
        turn_id="turn",
    )

    restored = Agent(llm=_LLM(), tools=[])
    restored.restore_history_runtime(
        SimpleNamespace(
            id="session",
            history_events=source.history_ledger.events,
            replay_envelope=None,
            request_envelopes=(),
            history_completeness="complete",
            checkpoints=(),
            messages=[],
        )
    )

    assert restored.take_recovered_steering_discard_count() == 1
    assert restored.messages == []
    assert restored.history_ledger.events[-1].payload == {
        "steering_id": steering_id,
        "reason": "session_recovery",
    }
