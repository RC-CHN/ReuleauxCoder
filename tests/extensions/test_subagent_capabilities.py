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
    UpdatePlanTool,
)
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
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
    assert explore == baseline | {"report_progress", "report_to_parent"}
    assert execute == baseline | {
        "write_file",
        "edit_file",
        "shell",
        "report_progress",
        "report_to_parent",
    }
    assert verify == baseline | {"shell", "report_progress", "report_to_parent"}
    assert "agent" not in execute
    assert "update_plan" not in execute


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
