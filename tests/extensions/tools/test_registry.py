from inspect import Parameter, signature

from reuleauxcoder.extensions.tools.backend import LocalToolBackend
from reuleauxcoder.extensions.tools.builtin import builtin_tool_types
from reuleauxcoder.extensions.tools.registry import build_tools, iter_tool_classes


EXPECTED_BUILTIN_TOOL_NAMES = (
    "update_plan",
    "report_progress",
    "report_to_parent",
    "request_guidance",
    "edit_file",
    "glob",
    "grep",
    "history_search",
    "history_read",
    "artifact_read",
    "list_file",
    "lsp",
    "lsp_status",
    "lsp_diagnostics",
    "lsp_restart",
    "write_note",
    "edit_note",
    "delete_note",
    "read_file",
    "shell",
    "shell_session",
    "spawn_agent",
    "send_message",
    "list_agents",
    "wait_agent",
    "interrupt_agent",
    "write_file",
    "web_fetch",
    "web_search",
)


def test_builtin_tool_contributions_have_stable_explicit_order() -> None:
    tool_types = builtin_tool_types()

    assert tool_types == iter_tool_classes()
    assert tuple(tool_type.name for tool_type in tool_types) == (
        EXPECTED_BUILTIN_TOOL_NAMES
    )
    assert len(set(tool_types)) == len(tool_types)


def test_build_tools_preserves_the_complete_builtin_schema_order() -> None:
    assert tuple(
        tool.name for tool in build_tools(LocalToolBackend())
    ) == EXPECTED_BUILTIN_TOOL_NAMES


def test_build_tools_requires_an_explicit_backend() -> None:
    assert signature(build_tools).parameters["backend"].default is Parameter.empty
