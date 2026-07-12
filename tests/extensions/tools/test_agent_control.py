from types import SimpleNamespace

from reuleauxcoder.extensions.subagent.manager import SubagentManager
from reuleauxcoder.extensions.tools.builtin.agent import AgentTool


def test_leaf_agent_keeps_reporting_but_cannot_spawn_deeper() -> None:
    manager = SubagentManager(parent_agent_id="root", max_depth=1)
    parent = SimpleNamespace(
        agent_id="child",
        subagent_depth=1,
        _subagent_manager=manager,
    )
    manager.register_child_agent(
        "child", 1, parent_agent_id="root", job_id="sj_child"
    )
    tool = AgentTool()
    tool._parent_agent = parent

    assert tool.preflight_validate(action="report", tasks=["blocked on scope"]) is None
    assert "depth limit" in (
        tool.preflight_validate(
            action="spawn",
            tasks=["nested work"],
            run_in_background=True,
        )
        or ""
    )
    assert "Progress reported" in tool._execute_local(
        action="report", tasks=["blocked on scope"]
    )
    assert manager.drain_parent_messages("root")[0].content == "blocked on scope"
    manager.shutdown()
