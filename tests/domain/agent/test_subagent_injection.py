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
