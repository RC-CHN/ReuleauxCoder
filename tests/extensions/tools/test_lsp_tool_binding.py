from unittest.mock import MagicMock

from reuleauxcoder.extensions.tools.builtin.lsp import LspTool


def test_lsp_tools_hold_instance_scoped_managers() -> None:
    first_manager = MagicMock()
    second_manager = MagicMock()

    first = LspTool(lsp_manager=first_manager)
    second = LspTool(lsp_manager=second_manager)

    assert first.lsp_manager is first_manager
    assert second.lsp_manager is second_manager

    first.bind_lsp_manager(None)

    assert first.lsp_manager is None
    assert second.lsp_manager is second_manager
