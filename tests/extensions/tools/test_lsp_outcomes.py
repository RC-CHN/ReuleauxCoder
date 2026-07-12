from __future__ import annotations

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcomeStatus
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool


class _EmptyLspManager:
    def send_request_sync(self, *_args, **_kwargs):
        return []


def test_lsp_success_has_full_model_content_and_compact_summary(tmp_path) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tool = LspTool(lsp_manager=_EmptyLspManager())

    outcome = tool.execute(
        operation="documentSymbol",
        filePath=str(source),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.SUCCEEDED
    assert outcome.summary
    assert outcome.summary != "None"
    assert outcome.model_text
    assert outcome.metadata["operation"] == "documentSymbol"


def test_lsp_failure_has_explicit_failed_status_and_summary(tmp_path) -> None:
    missing = tmp_path / "missing.py"
    outcome = LspTool().execute(
        operation="goToDefinition",
        filePath=str(missing),
        line=1,
        character=1,
    )

    assert outcome.status is ToolOutcomeStatus.FAILED
    assert outcome.summary
    assert "not found" in outcome.model_text.lower()
