from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.extensions.subagent.manager import SubagentManager
from reuleauxcoder.extensions.tools.builtin.control import (
    ReportProgressTool,
    ReportToParentTool,
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


def test_subagent_progress_updates_panel_state_without_parent_context_item() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.subagent_depth = 1
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent(
        agent.agent_id, 1, parent_agent_id="root", job_id="sj_child"
    )
    agent._subagent_manager = manager
    tool = _bind(ReportProgressTool(), agent, "call")

    outcome = tool.execute("investigating", "Inspecting parser", next="Read tests")

    assert outcome.success is True
    assert manager.drain_parent_messages("root") == []
    assert "blocked" in ReportProgressTool.parameters["properties"]["phase"]["enum"]
    manager.shutdown()


def test_subagent_can_report_blocked_progress_without_modifying_plan() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.subagent_depth = 1
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent(
        agent.agent_id, 1, parent_agent_id="root", job_id="sj_blocked"
    )
    agent._subagent_manager = manager
    tool = _bind(ReportProgressTool(), agent, "blocked-call")

    outcome = tool.execute("blocked", "Need a parser ownership decision")

    assert outcome.success is True
    assert agent.plan_controller.progress.phase == "blocked"
    assert agent.plan_controller.state.items == ()
    manager.shutdown()


def test_report_to_parent_queues_correlated_non_blocking_reply() -> None:
    agent = Agent(llm=_LLM(), tools=[])
    agent.subagent_depth = 1
    manager = SubagentManager(parent_agent_id="root")
    manager.register_child_agent(
        agent.agent_id, 1, parent_agent_id="root", job_id="sj_child"
    )
    agent._subagent_manager = manager
    tool = _bind(ReportToParentTool(), agent, "report-call")

    outcome = tool.execute(
        "The symbol is defined in parser.py",
        kind="reply",
        reply_to="sd_question",
    )

    assert outcome.success is True
    messages = manager.drain_parent_messages("root")
    assert len(messages) == 1
    assert messages[0].kind == "reply"
    assert messages[0].reply_to == "sd_question"
    assert messages[0].content == "The symbol is defined in parser.py"
    manager.shutdown()
