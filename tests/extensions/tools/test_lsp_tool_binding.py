from unittest.mock import MagicMock

import pytest

from reuleauxcoder.extensions.tools.builtin.lsp import (
    LspDiagnosticsTool,
    LspStatusTool,
    LspTool,
)


@pytest.mark.parametrize("tool_type", [LspTool, LspStatusTool, LspDiagnosticsTool])
def test_lsp_tools_hold_instance_scoped_managers(tool_type) -> None:
    first_manager = MagicMock()
    second_manager = MagicMock()

    first = tool_type(lsp_manager=first_manager)
    second = tool_type(lsp_manager=second_manager)

    assert first.lsp_manager is first_manager
    assert second.lsp_manager is second_manager

    first.bind_lsp_manager(None)

    assert first.lsp_manager is None
    assert second.lsp_manager is second_manager


@pytest.mark.parametrize("tool_type", [LspTool, LspStatusTool, LspDiagnosticsTool])
def test_lsp_tool_scope_clone_keeps_manager_without_sharing_instance(tool_type) -> None:
    manager = MagicMock()
    original = tool_type(lsp_manager=manager)

    clone = original.clone_for_scope("subagent")

    assert type(clone) is tool_type
    assert clone is not original
    assert clone.lsp_manager is manager
