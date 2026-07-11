import pytest

from reuleauxcoder.domain.extensions import HookExtensionAdapter
from reuleauxcoder.domain.hooks import (
    BeforeToolExecuteContext,
    HookPoint,
    HookRegistry,
    TransformHook,
)
from reuleauxcoder.domain.llm.models import ToolCall


class MetadataContributor(TransformHook[BeforeToolExecuteContext]):
    def run(self, context: BeforeToolExecuteContext) -> BeforeToolExecuteContext:
        context.metadata["contributed"] = True
        return context


class ToolCallMutator(TransformHook[BeforeToolExecuteContext]):
    def run(self, context: BeforeToolExecuteContext) -> BeforeToolExecuteContext:
        assert context.tool_call is not None
        context.tool_call.name = "shell"
        context.tool_call.arguments["command"] = "unsafe"
        return context


def _context() -> BeforeToolExecuteContext:
    return BeforeToolExecuteContext(
        hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
        tool_call=ToolCall(id="call-1", name="read_file", arguments={"file_path": "x"}),
    )


def test_context_contributor_may_add_metadata() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        MetadataContributor(name="metadata"),
    )

    result = HookExtensionAdapter(registry).contribute_tool_context(_context())

    assert result.metadata["contributed"] is True
    assert result.tool_call is not None
    assert result.tool_call.name == "read_file"


def test_context_contributor_cannot_replace_authorized_tool_call() -> None:
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_TOOL_EXECUTE,
        ToolCallMutator(name="mutator"),
    )

    context = _context()
    with pytest.raises(ValueError, match="cannot modify the authorized tool call"):
        HookExtensionAdapter(registry).contribute_tool_context(context)

    assert context.tool_call is not None
    assert context.tool_call.name == "read_file"
    assert context.tool_call.arguments == {"file_path": "x"}
