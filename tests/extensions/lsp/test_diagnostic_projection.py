from pathlib import Path
from xml.etree import ElementTree

import pytest

from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.diagnostic_projection import (
    project_diagnostic_results,
)
from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
    render_blocks,
)


def _batch(path: str, items: list[Diagnostic]) -> DiagnosticBatch:
    return DiagnosticBatch(
        route=DiagnosticRoute(file_path=Path(path)),
        request_sequence=1,
        document_version=1,
        diagnostic_generation=1,
        block=DiagnosticBlock(path, items),
    )


def test_severity_precedes_per_file_cap_and_feedback_counts_only_selected_items():
    batch = _batch(
        "main.py",
        [
            *[Diagnostic(i + 1, 1, "warning", severity=2) for i in range(20)],
            Diagnostic(21, 1, "important error"),
        ],
    )
    full = render_blocks([batch.block], max_diagnostics=20, include_warnings=True)
    assert "important error" in full
    assert full.count("WARNING") == 19
    projected = project_diagnostic_results((batch,), (), LspConfig())
    assert projected.summary == "1 error, 19 warnings, 1 omitted"
    assert "1 diagnostics and 0 outcomes omitted" in projected.text
    assert len(batch.block.items) == 21


@pytest.mark.parametrize("budget", [512, 1200, 12_000])
def test_full_xml_projection_is_bounded_and_preserves_raw_diagnostics(budget: int):
    message = "<&>" * 400_000
    batches = tuple(
        _batch(f'file-{i}"<>.py', [Diagnostic(1, 1, message)]) for i in range(32)
    )
    outcomes = tuple(
        DiagnosticOutcome(
            batch_id=f"stale-{i}",
            route=DiagnosticRoute(file_path=Path("other.py")),
            request_sequence=i,
            status=DiagnosticOutcomeStatus.STALE_DISCARDED,
            created_at=0,
        )
        for i in range(32)
    )
    result = project_diagnostic_results(
        batches, outcomes, LspConfig(max_injection_chars=budget)
    )
    assert result.text is not None
    assert len(result.text) <= budget
    assert "truncated:" in result.text
    ElementTree.fromstring(result.text.removeprefix("[LSP DIAGNOSTICS]\n"))
    assert all(batch.block.items[0].message == message for batch in batches)


def test_global_budget_prioritizes_errors_and_warn_filter_does_not_claim_delivery():
    warning = _batch("a.py", [Diagnostic(1, 1, "w" * 200, severity=2)])
    error = _batch("z.py", [Diagnostic(1, 1, "e" * 200)])
    result = project_diagnostic_results(
        (warning, error), (), LspConfig(max_injection_chars=512)
    )
    assert "ERROR" in result.text
    assert "WARNING" not in result.text
    assert result.summary == "1 error, 1 omitted"
    filtered = project_diagnostic_results(
        (warning,), (), LspConfig(include_warnings=False)
    )
    assert filtered.text is None
    assert filtered.summary == ""


def test_message_limit_applies_before_escaping_and_marks_shortened_messages():
    batch = _batch("main.py", [Diagnostic(1, 1, "<&>" * 1000)])
    result = project_diagnostic_results((batch,), (), LspConfig(max_message_chars=10))
    root = ElementTree.fromstring(result.text.removeprefix("[LSP DIAGNOSTICS]\n"))
    assert "<&><&><&>…" in root.find("diagnostics").text
    assert result.summary == "1 error, 1 shortened"
