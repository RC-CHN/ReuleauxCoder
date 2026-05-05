from types import SimpleNamespace

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEventType


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
    assert "[Background sub-agent completed]" in agent.state.messages[-1]["content"]
    assert "done" in agent.state.messages[-1]["content"]
    assert [event.event_type for event in events] == [
        AgentEventType.SUBAGENT_COMPLETED,
        AgentEventType.TOOL_CALL_END,
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
    agent.state.messages.append({
        "role": "assistant",
        "content": "calling tool...",
        "tool_calls": [
            {
                "id": "call_pending_001",
                "type": "function",
                "function": {"name": "shell", "arguments": "{}"},
            }
        ],
    })

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
    agent.state.messages.append({
        "role": "tool",
        "tool_call_id": "call_pending_001",
        "content": "ok",
    })
    flushed = agent._flush_pending_subagent_injections()
    assert flushed == 1

    # Now the sub-agent result must be in messages.
    assert agent.state.messages[-1]["role"] == "assistant"
    assert "[Background sub-agent completed]" in agent.state.messages[-1]["content"]
    assert "done" in agent.state.messages[-1]["content"]

    # Events should have been emitted during flush.
    assert [e.event_type for e in events] == [
        AgentEventType.SUBAGENT_COMPLETED,
        AgentEventType.TOOL_CALL_END,
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


def test_flush_empty_is_noop() -> None:
    agent = _make_agent()
    assert agent._flush_pending_subagent_injections() == 0
    assert agent.state.messages == []
