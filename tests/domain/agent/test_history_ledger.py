from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.context.replay import ReplayEnvelope
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
    assert events[1].kind == "context_view_committed"
    assert agent.messages == [{"role": "system", "content": "checkpoint"}]


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
    assert "<system_context>" in request_event.payload["overlay"]["content"]
    assert all(
        "<system_context>" not in str(item.get("content")) for item in replay.items
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


def test_reset_preserves_ledger_truth_but_commits_empty_runtime_view() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._append_message({"role": "user", "content": "keep truth"}, source="user")

    agent.reset()

    assert agent.messages == []
    kinds = [event.kind for event in agent.history_ledger.events]
    assert kinds == ["message_committed", "runtime_reset", "context_view_committed"]
    assert agent.context.cache_epoch == 1
