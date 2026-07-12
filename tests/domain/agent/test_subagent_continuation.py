import threading

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.llm.models import LLMResponse
from reuleauxcoder.extensions.subagent.manager import SubagentManager


class _ContinuationLLM:
    model = "test-model"
    debug_trace = False

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.calls = []

    def chat(self, *, messages, **kwargs):
        self.calls.append((messages, kwargs.get("metadata")))
        if len(self.calls) == 1:
            self.release.set()
            return LLMResponse(content="I will wait.")
        return LLMResponse(content="Child result incorporated.")


def test_awaited_child_completion_continues_same_root_turn(monkeypatch) -> None:
    release = threading.Event()

    def child(**kwargs):
        release.wait(timeout=2)
        return "child evidence"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.subagent.manager.run_subagent_task", child
    )
    llm = _ContinuationLLM(release)
    agent = Agent(llm=llm, tools=[], max_rounds=5)
    manager = SubagentManager(max_parallel_explore=1, parent_agent_id=agent.agent_id)
    agent._subagent_manager = manager
    manager.submit_background(parent_agent=agent, task="inspect", mode="explore")

    result = agent.chat("Please inspect then answer.")

    assert result == "Child result incorporated."
    assert len(llm.calls) == 2
    assert llm.calls[0][1]["turn_id"] == llm.calls[1][1]["turn_id"]
    second_messages = llm.calls[1][0]
    assert any(
        message.get("role") == "system"
        and "child evidence" in str(message.get("content"))
        for message in second_messages
    )
    manager.shutdown()
