from types import SimpleNamespace
from unittest.mock import MagicMock

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.extensions.subagent.manager import SubagentManager


class _LLMStub:
    model = "stub-model"


def _make_agent() -> Agent:
    return Agent(llm=_LLMStub(), tools=[])


def test_inject_subagent_job_result_appends_message_and_emits_events() -> None:
    agent = _make_agent()
    events = []
    agent.add_event_handler(events.append)

    job = SimpleNamespace(
        id="sj_1",
        mode="explore",
        task="scan repo",
        status="completed",
        result="done",
        error=None,
        injected_to_parent=False,
    )

    injected = agent.inject_subagent_job_result(job)

    assert injected is True
    assert job.injected_to_parent is True
    assert agent.state.messages[-1]["role"] == "assistant"
    assert "[Sub-agent result notification]" in agent.state.messages[-1]["content"]
    assert "done" in agent.state.messages[-1]["content"]
    assert [event.event_type for event in events] == [
        AgentEventType.SUBAGENT_COMPLETED,
    ]


def test_inject_subagent_job_result_is_idempotent() -> None:
    agent = _make_agent()

    job = SimpleNamespace(
        id="sj_1",
        mode="explore",
        task="scan repo",
        status="completed",
        result="done",
        error=None,
        injected_to_parent=False,
    )

    assert agent.inject_subagent_job_result(job) is True
    before = list(agent.state.messages)
    assert agent.inject_subagent_job_result(job) is False
    assert agent.state.messages == before


def test_inject_defers_when_pending_tool_calls_exist() -> None:
    """Sub-agent injection must be buffered, not interleaved, when there are
    unresolved tool_calls in the message history.

    Regression test: without buffering, background sub-agent results
    injected between an assistant tool_calls message and its tool response
    violate the LLM API contract and cause 400 errors.
    """
    agent = _make_agent()
    events = []
    agent.add_event_handler(events.append)

    # Simulate a pending tool call that hasn't been responded to yet.
    agent.state.messages.append(
        {
            "role": "assistant",
            "content": "calling tool...",
            "tool_calls": [
                {
                    "id": "call_pending_001",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ],
        }
    )

    job = SimpleNamespace(
        id="sj_bg_1",
        mode="explore",
        task="scan repo",
        status="completed",
        result="done",
        error=None,
        injected_to_parent=False,
    )

    injected = agent.inject_subagent_job_result(job)

    # The injection should be accepted (not dropped), but buffered.
    assert injected is True
    assert job.injected_to_parent is True

    # The sub-agent result must NOT appear in messages yet.
    for msg in agent.state.messages:
        assert "[Background sub-agent" not in str(msg.get("content", ""))

    # Events must NOT be emitted while buffered.
    assert len(events) == 0

    # After resolving the pending tool call, flushing should release it.
    agent.state.messages.append(
        {
            "role": "tool",
            "tool_call_id": "call_pending_001",
            "content": "ok",
        }
    )
    flushed = agent._flush_pending_subagent_injections()
    assert flushed == 1

    # Now the sub-agent result must be in messages.
    assert agent.state.messages[-1]["role"] == "assistant"
    assert "[Sub-agent result notification]" in agent.state.messages[-1]["content"]
    assert "done" in agent.state.messages[-1]["content"]

    # Events should have been emitted during flush.
    assert [e.event_type for e in events] == [
        AgentEventType.SUBAGENT_COMPLETED,
    ]


def test_inject_direct_when_no_pending_tool_calls() -> None:
    """When the message history has no unresolved tool_calls, injection
    should append directly to messages (no buffering)."""
    agent = _make_agent()

    # Clean state: no pending tool calls.
    assert agent._collect_pending_tool_calls() == []

    job = SimpleNamespace(
        id="sj_direct",
        mode="explore",
        task="scan repo",
        status="completed",
        result="done",
        error=None,
        injected_to_parent=False,
    )

    injected = agent.inject_subagent_job_result(job)
    assert injected is True
    assert agent.state.messages[-1]["role"] == "assistant"
    assert "done" in agent.state.messages[-1]["content"]
    # Buffer should remain empty.
    assert agent._pending_subagent_injections == []


def test_reconcile_moves_recovered_tool_outputs_before_session_markers() -> None:
    agent = _make_agent()
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_a",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            },
            {
                "id": "call_b",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            },
        ],
    }
    agent.state.messages[:] = [
        assistant,
        {"role": "tool", "tool_call_id": "call_a", "content": "A"},
        {"role": "user", "content": "[SESSION_EXIT]"},
        {"role": "tool", "tool_call_id": "call_b", "content": "B"},
    ]

    synthesized = agent.reconcile_pending_tool_calls()

    assert synthesized == 0
    assert [message["role"] for message in agent.state.messages] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert agent._collect_pending_tool_calls() == []


def test_flush_empty_is_noop() -> None:
    agent = _make_agent()
    assert agent._flush_pending_subagent_injections() == 0
    assert agent.state.messages == []


def test_reset_advances_generation_and_clears_pending_injections() -> None:
    agent = _make_agent()
    manager = SubagentManager()
    agent._subagent_manager = manager
    agent._pending_subagent_injections.append((object(), "old", True))
    cancelled = []
    agent.ui_interactor = SimpleNamespace(
        cancel_all=lambda *, reason: cancelled.append(reason)
    )

    agent.reset()

    assert manager.generation == 1
    assert agent._pending_subagent_injections == []
    assert cancelled == ["session reset"]
    manager.shutdown()


def test_reset_advances_lsp_generation_watermark() -> None:
    agent = _make_agent()
    manager = MagicMock()
    agent.lsp_manager = manager

    agent.reset()

    manager.advance_session_generation.assert_called_once_with(
        agent.agent_id, agent.session_generation
    )


def test_old_generation_job_is_rejected_by_agent_injection() -> None:
    agent = _make_agent()
    manager = SubagentManager()
    agent._subagent_manager = manager
    manager.advance_generation(cancel_pending=False)
    job = SimpleNamespace(
        id="old",
        mode="explore",
        task="old",
        status="completed",
        result="stale",
        error=None,
        injected_to_parent=False,
        generation=0,
    )

    assert agent.inject_subagent_job_result(job) is False
    assert agent.state.messages == []
    manager.shutdown()
