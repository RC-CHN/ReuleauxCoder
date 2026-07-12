from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.extensions.subagent.manager import SubagentManager
from reuleauxcoder.extensions.tools.builtin.control import (
    ReportProgressTool,
    UpdatePlanTool,
)


class _LLM:
    model = "model"


def _bind(tool, agent, call_id):
    tool.bind_agent(agent)
    tool.bind_execution(
        tool_call_id=call_id, session_generation=agent.session_generation
    )
    return tool


def test_update_plan_tool_returns_concise_result() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    tool = _bind(UpdatePlanTool(), agent, "call")
    outcome = tool.execute(
        [{"step": "Test", "active_form": "Testing", "status": "in_progress"}]
    )

    assert outcome.success is True
    assert "1 active" in outcome.content
    assert "Testing" not in outcome.content
    assert agent.plan_controller.state.revision == 1


def test_blocked_subagent_progress_notifies_immediate_parent() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.subagent_depth = 1
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent(
        agent.agent_id, 1, parent_agent_id="root", job_id="sj_child"
    )
    agent._subagent_manager = manager
    tool = _bind(ReportProgressTool(), agent, "call")

    outcome = tool.execute("blocked", "Need product decision", next="Ask user")

    assert outcome.success is True
    messages = manager.drain_parent_messages("root")
    assert len(messages) == 1
    assert messages[0].kind == "blocked"
    assert messages[0].content == "Need product decision"
    manager.shutdown()
