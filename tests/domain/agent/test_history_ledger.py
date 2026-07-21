import json
from pathlib import Path

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome, ToolOutcomeStatus
from reuleauxcoder.domain.context.replay import ReplayEnvelope, content_hash
from reuleauxcoder.domain.history import HistoryLedger
from reuleauxcoder.domain.session.models import Session
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


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
    agent._append_message({"role": "user", "content": "hello"}, source="user_input")

    event = agent.history_ledger.events[-1]
    encoded = event.to_dict()
    assert event.schema_version == 2
    assert event.session_id == "session-1"
    assert event.agent_id == agent.agent_id
    assert event.turn_id == "turn-7"
    assert event.api_round_id == "turn-7:2"
    assert event.role == "user"
    assert encoded["timestamp"] == event.created_at


def test_structured_tool_lifecycle_is_persisted_as_runtime_truth() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._current_turn_id = "turn"
    agent._emit_event(
        AgentEvent.tool_call_start("shell", {"command": "false"}, tool_call_id="tc")
    )
    agent._emit_event(
        AgentEvent.tool_call_end(
            "shell",
            "failed",
            tool_call_id="tc",
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                summary="command failed",
                exit_code=1,
            ),
        )
    )

    started, finished = agent.history_ledger.events
    assert started.kind == "tool_call_started"
    assert started.payload["arguments"] == {"command": "false"}
    assert finished.kind == "tool_call_finished"
    assert finished.payload["status"] == "failed"
    assert finished.payload["exit_code"] == 1


def test_request_audit_keeps_overlay_out_of_replay_items() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent._append_message({"role": "user", "content": "do work"}, source="user_input")
    request_messages = agent._loop._full_messages()

    agent._loop._record_request_envelopes(request_messages, [])

    replay = agent.replay_envelope
    assert replay is not None and replay.validate()
    assert list(replay.items) == [{"role": "user", "content": "do work"}]
    assert len(replay.item_provenance) == len(replay.items)
    assert replay.item_provenance[0]["source_event_ids"]
    request_event = agent.history_ledger.events[-1]
    assert request_event.kind == "request_committed"
    assert "<execution_state" in request_event.payload["overlay"]["content"]
    assert request_event.payload["replay"]["item_count"] == 1
    assert "items" not in request_event.payload["replay"]
    assert all(
        "<execution_state" not in str(item.get("content")) for item in replay.items
    )


def test_request_audit_hashes_exact_dispatched_payload() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    messages = agent._loop._full_messages()
    payload = {
        "model": "hook-selected-model",
        "messages": messages,
        "tools": [],
        "stream": True,
        "temperature": 0.25,
        "stream_options": {"include_usage": True},
    }

    agent._loop._record_request_envelopes(
        messages,
        [],
        request_settings={
            "stream": True,
            "temperature": 0.25,
            "stream_options": {"include_usage": True},
        },
        model_profile="hook-selected-model",
        canonical_request_payload=payload,
    )

    replay = agent.replay_envelope
    assert replay is not None
    assert replay.request_settings["dispatched"]["temperature"] == 0.25
    assert "configured" in replay.request_settings
    assert agent.request_envelopes[-1].canonical_request_hash == content_hash(payload)


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


def test_legacy_event_migration_preserves_longest_resume_request_prefix(
    tmp_path: Path,
) -> None:
    original_agent = Agent(llm=_LLM(), tools=[])
    system = original_agent._loop._full_messages()[0]
    items = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "continue from here"},
    ]
    settings = original_agent._loop._wire_settings()
    replay = ReplayEnvelope.create(
        session_id="session-prefix",
        cache_epoch=4,
        history_version=9,
        model_profile="test-model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        request_settings={"configured": settings, "dispatched": settings},
        instructions=[system],
        tools=[],
        items=items,
    )
    ledger = HistoryLedger()
    legacy_request = ledger.append(
        "request_committed",
        {"request": {}, "replay": replay.to_dict(), "overlay": {}},
    )
    store = SessionStore(tmp_path)
    store.save(
        messages=items,
        model="test-model",
        session_id="session-prefix",
        history_events=list(ledger.events),
        replay_envelope=replay,
    )
    # Recreate the pre-v2 on-disk event shape; normal writes already compact it.
    events_path = tmp_path / "session-prefix" / "events.jsonl"
    events_path.write_text(
        json.dumps(legacy_request.to_dict(), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    loaded = store.load("session-prefix")
    assert loaded is not None and loaded.replay_envelope is not None
    resumed_agent = Agent(llm=_LLM(), tools=[])
    resumed_agent.restore_history_runtime(loaded)
    next_request = resumed_agent._loop._full_messages()
    previous_stable_prefix = [dict(system), *items]
    matched = 0
    for previous, current in zip(previous_stable_prefix, next_request):
        if previous != current:
            break
        matched += 1

    assert matched == len(previous_stable_prefix)
    assert len(next_request) > matched
    assert loaded.replay_envelope.stable_prefix_hash == replay.stable_prefix_hash
    assert resumed_agent.context.cache_epoch == 4
    assert resumed_agent.context.history_version == 9


def test_resume_migrates_legacy_system_prefix_once() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    replay = ReplayEnvelope.create(
        session_id="session",
        cache_epoch=3,
        history_version=7,
        model_profile="old-model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        request_settings={},
        instructions=[{"role": "system", "content": "old stable instructions"}],
        tools=[],
        items=[{"role": "user", "content": "restored user message"}],
    )
    session = Session(
        id="session",
        model="old-model",
        saved_at="now",
        messages=list(replay.items),
        replay_envelope=replay,
        history_completeness="complete",
    )
    agent.restore_history_runtime(session)

    first = agent._loop._full_messages()
    second = agent._loop._full_messages()

    assert first[0]["role"] == "system"
    assert "# Runtime Context Protocol" in first[0]["content"]
    assert first[1] == {"role": "user", "content": "restored user message"}
    assert second[0] == first[0]
    assert agent._restored_replay_envelope is None
    assert agent.context.history_version == 8
    assert agent.context.cache_epoch == 4
    migrations = [
        event
        for event in agent.history_ledger.events
        if event.kind == "stable_context_updated"
    ]
    assert len(migrations) == 1


def test_resume_does_not_emit_false_update_for_matching_configured_settings() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    current_system = agent._loop._full_messages()[0]
    replay = ReplayEnvelope.create(
        session_id="session",
        cache_epoch=0,
        history_version=0,
        model_profile="test-model",
        provider_family="openai-compatible",
        request_mode="chat-completions",
        request_settings={
            "configured": agent._loop._wire_settings(),
            "dispatched": {"stream": True},
        },
        instructions=[current_system],
        tools=[],
        items=[{"role": "user", "content": "restored"}],
        item_provenance=[{}],
    )
    agent.restore_history_runtime(
        Session(
            id="session",
            model="test-model",
            saved_at="now",
            messages=list(replay.items),
            replay_envelope=replay,
            history_completeness="complete",
        )
    )

    messages = agent._loop._full_messages()

    assert messages[0] == current_system
    assert not any(
        str(item.get("content") or "").startswith("[Runtime context update]")
        for item in agent.messages
    )


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

    assert overlay["role"] == "user"
    assert 'plan_revision="1"' in overlay["content"]
    assert overlay["content"].count("</execution_data>") == 1
    assert "\\u003c/execution_data\\u003e" in overlay["content"]
    assert all("<execution_state" not in str(item) for item in agent.messages)
    assert agent.request_envelopes[-1].plan_revision == 1
