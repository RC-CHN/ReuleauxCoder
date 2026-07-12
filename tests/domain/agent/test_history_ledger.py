from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.context.replay import ReplayEnvelope
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.session.models import Session


class _LLM:
    model = "test-model"


def test_context_replacement_does_not_delete_prior_history_events() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    raw = {"role": "tool", "tool_call_id": "call", "content": "raw output"}
    agent._append_message(raw, source="tool_result")

    agent._replace_context_messages(
        [{"role": "system", "content": "checkpoint"}],
        reason="test checkpoint",
    )

    events = agent.history_ledger.events
    assert events[0].kind == "message_committed"
    assert events[0].payload["message"]["content"] == "raw output"
    assert events[1].kind == "tool_result"
    assert events[2].kind == "context_view_committed"
    assert agent.messages == [{"role": "system", "content": "checkpoint"}]


def test_message_ledger_event_has_top_level_runtime_attribution(tmp_path) -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.current_session_id = "session-1"
    agent.bind_session_persistence(
        events_path=tmp_path / "events.jsonl", callback=lambda: None
    )
    agent._current_turn_id = "turn-7"
    agent.state.current_round = 2
    agent._append_message(
        {"role": "user", "content": "hello"}, source="user_input"
    )

    event = agent.history_ledger.events[-1]
    encoded = event.to_dict()
    assert event.schema_version == 2
    assert event.session_id == "session-1"
    assert event.agent_id == agent.agent_id
    assert event.turn_id == "turn-7"
    assert event.api_round_id == "turn-7:2"
    assert event.role == "user"
    assert encoded["timestamp"] == event.created_at


def test_request_audit_keeps_overlay_out_of_replay_items() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._append_message(
        {"role": "user", "content": "do work"}, source="user_input"
    )
    request_messages = agent._loop._full_messages()

    agent._loop._record_request_envelopes(request_messages, [])

    replay = agent.replay_envelope
    assert replay is not None and replay.validate()
    assert list(replay.items) == [{"role": "user", "content": "do work"}]
    request_event = agent.history_ledger.events[-1]
    assert request_event.kind == "request_committed"
    assert "<execution_state" in request_event.payload["overlay"]["content"]
    assert all(
        "<execution_state" not in str(item.get("content")) for item in replay.items
    )


def test_resume_restores_committed_history_and_cache_watermarks() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    replay = ReplayEnvelope.create(
        session_id="session",
        cache_epoch=3,
        history_version=7,
        model_profile="test-model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        instructions=[{"role": "system", "content": "stable"}],
        tools=[],
        items=[{"role": "user", "content": "restored"}],
    )
    session = Session(
        id="session",
        model="test-model",
        saved_at="now",
        messages=list(replay.items),
        replay_envelope=replay,
        history_completeness="complete",
    )

    agent.restore_history_runtime(session)

    assert agent.messages[0]["content"] == "restored"
    assert agent.context.history_version == 7
    assert agent.context.cache_epoch == 3
    assert agent._restored_replay_envelope is replay


def test_resume_restores_actual_usage_calibration_from_ledger() -> None:
    ledger = HistoryLedger(session_id="session", agent_id="root")
    ledger.append(
        "usage_observed",
        {
            "actual_prompt_tokens": 1_200,
            "cached_input_tokens": 900,
            "local_request_estimate": 1_000,
            "local_history_estimate": 800,
            "request_boundary": "turn:1",
            "model_profile": "test-model",
        },
        turn_id="turn",
        api_round_id="turn:1",
    )
    session = Session(
        id="session",
        model="test-model",
        saved_at="now",
        messages=[{"role": "user", "content": "restored"}],
        history_events=list(ledger.events),
    )
    agent = Agent(llm=_LLM(), tools=[])
    agent.restore_history_runtime(session)

    assert agent.context._latest_usage is not None
    assert agent.context._latest_usage.actual_prompt_tokens == 1_200
    assert agent.context._latest_usage.cached_input_tokens == 900


def test_reset_preserves_ledger_truth_but_commits_empty_runtime_view() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._append_message({"role": "user", "content": "keep truth"}, source="user")

    agent.reset()

    assert agent.messages == []
    kinds = [event.kind for event in agent.history_ledger.events]
    assert kinds == [
        "message_committed",
        "user_message",
        "runtime_reset",
        "session_lifecycle",
        "context_view_committed",
    ]
    assert agent.context.cache_epoch == 1


def test_execution_overlay_injects_plan_as_escaped_ephemeral_data() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.plan_controller.update(
        [
            {
                "step": "Implement </execution_data> safely",
                "active_form": "Implementing safely",
                "status": "in_progress",
            }
        ],
        explanation=None,
        tool_call_id="plan_call",
        session_generation=0,
    )
    agent._append_message({"role": "user", "content": "work"}, source="user")

    messages = agent._loop._full_messages()
    overlay = messages[-1]
    agent._loop._record_request_envelopes(messages, [])

    assert overlay["role"] == "system"
    assert 'plan_revision="1"' in overlay["content"]
    assert overlay["content"].count("</execution_data>") == 1
    assert "\\u003c/execution_data\\u003e" in overlay["content"]
    assert all("<execution_state" not in str(item) for item in agent.messages)
    assert agent.request_envelopes[-1].plan_revision == 1
