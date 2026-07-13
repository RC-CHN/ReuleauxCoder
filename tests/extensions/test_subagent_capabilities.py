from __future__ import annotations

from types import SimpleNamespace

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ApprovalConfig
from reuleauxcoder.domain.hooks.builtin.tool_policy import ToolPolicyGuardHook
from reuleauxcoder.domain.hooks.types import BeforeToolExecuteContext, HookPoint
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.subagent.manager import _filter_subagent_tools
from reuleauxcoder.extensions.subagent.scoped_tools import materialize_subagent_tool
from reuleauxcoder.extensions.tools.backend import LocalToolBackend
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.builtin.control import (
    ReportProgressTool,
    ReportToParentTool,
    RequestGuidanceTool,
    UpdatePlanTool,
)
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.subagent_control import (
    ListAgentsTool,
    SpawnAgentTool,
)
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool


class _LLM:
    model = "test"


class _RecordingShell(Tool):
    name = "shell"
    description = "record arguments"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def __init__(self, backend=None):
        super().__init__(backend or LocalToolBackend())
        self.received = None

    def execute(self, **kwargs):
        self.received = kwargs
        return "ok"


def _parent_with_all_tools():
    return SimpleNamespace(
        tools=[
            ReadFileTool(),
            ListFileTool(),
            GlobTool(),
            GrepTool(),
            LspTool(),
            WriteFileTool(),
            EditFileTool(),
            ShellTool(),
            UpdatePlanTool(),
            ReportProgressTool(),
            ReportToParentTool(),
        ]
    )


def test_child_capability_matrix_has_read_baseline_without_recursion_or_plan() -> None:
    parent = _parent_with_all_tools()

    explore = {tool.name for tool in _filter_subagent_tools(parent, "explore")}
    execute = {tool.name for tool in _filter_subagent_tools(parent, "execute")}
    verify = {tool.name for tool in _filter_subagent_tools(parent, "verify")}

    baseline = {"read_file", "list_file", "glob", "grep", "lsp"}
    controls = {"report_progress", "report_to_parent", "request_guidance"}
    assert explore == baseline | controls
    assert execute == baseline | {
        "write_file",
        "edit_file",
        "shell",
        *controls,
    }
    assert verify == baseline | {"shell", *controls}
    assert "agent" not in execute
    assert "update_plan" not in execute


def test_child_control_trio_does_not_depend_on_root_mode_tool_list() -> None:
    parent = SimpleNamespace(
        tools=[ReadFileTool(), ReportProgressTool(), UpdatePlanTool()]
    )

    child_tools = {tool.name: tool for tool in _filter_subagent_tools(parent, "explore")}

    assert set(child_tools) == {
        "read_file",
        "report_progress",
        "report_to_parent",
        "request_guidance",
    }
    assert isinstance(child_tools["report_to_parent"]._inner, ReportToParentTool)
    assert isinstance(child_tools["request_guidance"]._inner, RequestGuidanceTool)


def test_root_and_child_controls_are_filtered_before_schema_projection() -> None:
    tools = [
        UpdatePlanTool(),
        ReportProgressTool(),
        ReportToParentTool(),
        RequestGuidanceTool(),
        SpawnAgentTool(),
        ListAgentsTool(),
    ]
    agent = Agent(llm=_LLM(), tools=tools)

    assert {tool.name for tool in agent.get_active_tools()} == {
        "update_plan",
        "report_progress",
        "spawn_agent",
        "list_agents",
    }
    assert agent.suggest_modes_for_tool("report_to_parent") == []
    assert agent.is_tool_allowed_in_mode("request_guidance") is False
    assert agent.get_tool("request_guidance") is None

    agent.subagent_depth = 1
    assert {tool.name for tool in agent.get_active_tools()} == {
        "report_progress",
        "report_to_parent",
        "request_guidance",
    }
    assert agent.is_tool_allowed_in_mode("update_plan") is False
    assert agent.get_tool("spawn_agent") is None


def test_root_cannot_recover_child_only_control_from_global_registry() -> None:
    agent = Agent(llm=_LLM(), tools=[ReportToParentTool()])

    result = ToolExecutor(agent).execute(
        ToolCall(
            id="hallucinated-child-control",
            name="report_to_parent",
            arguments={"message": "done", "kind": "milestone"},
        )
    )

    assert result == (
        "Tool 'report_to_parent' is not available in current mode 'default'"
    )


def test_effectful_child_schema_requires_reason_and_strips_it_before_primitive() -> None:
    scoped = materialize_subagent_tool(_RecordingShell())

    schema = scoped.schema()["function"]["parameters"]
    assert "reason" in schema["required"]
    assert scoped.effect_class == "process_execution"
    assert scoped.preflight_validate(command="true") == (
        "Error: child tool 'shell' requires a non-empty reason."
    )

    assert scoped.execute(command="true", reason="verify the implementation") == "ok"
    assert scoped._inner.received == {"command": "true"}


def test_child_read_baseline_is_marked_for_deterministic_auto_approval() -> None:
    scoped = materialize_subagent_tool(ReadFileTool())
    hook = ToolPolicyGuardHook(
        approval_config=ApprovalConfig(default_mode="require_approval")
    )
    decision = hook.run(
        BeforeToolExecuteContext(
            hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
            tool_call=ToolCall(
                id="read-1",
                name="read_file",
                arguments={"file_path": "README.md"},
            ),
            metadata={
                "tool_source": scoped.tool_source,
                "effect_class": scoped.effect_class,
                "profile": scoped.approval_profile,
                "tool_schema": scoped.parameters,
            },
        )
    )

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_strict_child_scope_cannot_fall_back_to_global_registry() -> None:
    child = Agent(llm=_LLM(), tools=[])
    child.strict_tool_scope = True

    result = ToolExecutor(child).execute(
        ToolCall(id="illegal-plan", name="update_plan", arguments={"plan": []})
    )

    assert result == "Error: unknown tool 'update_plan'"
