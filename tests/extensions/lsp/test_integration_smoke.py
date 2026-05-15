"""Opt-in LSP integration smoke tests.

These tests start real language servers using the same command mapping used by
runtime LSP integration, create temporary broken source files, and assert that
LSP diagnostics can be collected.

Run with:
    RCODER_RUN_LSP_INTEGRATION=1 uv run python -m pytest tests/extensions/lsp/test_integration_smoke.py -q -s
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from reuleauxcoder.extensions.lsp.client import LspClient
from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic
from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    get_language_id_string,
    get_server_command,
)

RUN_LSP_INTEGRATION = os.environ.get("RCODER_RUN_LSP_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_LSP_INTEGRATION,
    reason="Set RCODER_RUN_LSP_INTEGRATION=1 to run real LSP smoke tests.",
)


@dataclass(frozen=True, slots=True)
class DiagnosticCase:
    language: LanguageId
    filename: str
    content: str
    expected_messages: tuple[str, ...]


DIAGNOSTIC_CASES: tuple[DiagnosticCase, ...] = (
    DiagnosticCase(
        language=LanguageId.PYTHON,
        filename="broken.py",
        content=(
            'def greet(name: str) -> str:\n'
            '    return f"Hello, {name}"\n'
            "\n"
            'print(greet("World")\n'
        ),
        expected_messages=("not closed",),
    ),
    DiagnosticCase(
        language=LanguageId.TYPESCRIPT,
        filename="broken.ts",
        content='const count: number = "oops";\nfunction f( {\n',
        expected_messages=("Type 'string' is not assignable", "'}' expected"),
    ),
    DiagnosticCase(
        language=LanguageId.JAVASCRIPT,
        filename="broken.js",
        content='function f( {\nconsole.log("oops")\n',
        expected_messages=("',' expected",),
    ),
    DiagnosticCase(
        language=LanguageId.YAML,
        filename="broken.yaml",
        content="root:\n  child: [1, 2\n",
        expected_messages=("Flow sequence", "end with a ]"),
    ),
)


STARTUP_ONLY_LANGUAGES: tuple[LanguageId, ...] = (
    LanguageId.BASH,
)


async def _collect_non_empty_diagnostics(
    client: LspClient,
    file_path: Path,
    *,
    timeout: float = 20.0,
) -> list[Diagnostic]:
    """Wait until the LSP publishes at least one diagnostic for a file."""
    deadline = time.monotonic() + timeout
    diagnostics: list[Diagnostic] = []

    while time.monotonic() < deadline:
        remaining = max(0.1, min(1.0, deadline - time.monotonic()))
        diagnostics = await client.wait_for_diagnostics(file_path, timeout=remaining)
        if diagnostics:
            return diagnostics

    return diagnostics


async def _run_diagnostic_case(case: DiagnosticCase, tmp_path: Path) -> list[Diagnostic]:
    cmd, args = get_server_command(case.language)
    if shutil.which(cmd) is None:
        pytest.skip(f"{cmd} is not available on PATH")

    file_path = tmp_path / case.filename
    file_path.write_text(case.content, encoding="utf-8")

    client = LspClient(language_id=case.language, workspace_root=tmp_path)
    try:
        await asyncio.wait_for(client.spawn(cmd, args), timeout=30.0)
        await asyncio.wait_for(client.initialize(), timeout=30.0)
        await client.did_open(file_path, case.content)
        return await _collect_non_empty_diagnostics(
            client,
            file_path,
            timeout=20.0,
        )
    finally:
        await asyncio.wait_for(client.shutdown(), timeout=10.0)


async def _run_startup_smoke(language: LanguageId, tmp_path: Path) -> None:
    cmd, args = get_server_command(language)
    if shutil.which(cmd) is None:
        pytest.skip(f"{cmd} is not available on PATH")

    client = LspClient(language_id=language, workspace_root=tmp_path)
    try:
        await asyncio.wait_for(client.spawn(cmd, args), timeout=30.0)
        await asyncio.wait_for(client.initialize(), timeout=30.0)
    finally:
        await asyncio.wait_for(client.shutdown(), timeout=10.0)


@pytest.mark.parametrize(
    "case",
    DIAGNOSTIC_CASES,
    ids=lambda case: get_language_id_string(case.language),
)
def test_installed_lsp_reports_diagnostics(
    case: DiagnosticCase,
    tmp_path: Path,
) -> None:
    diagnostics = asyncio.run(_run_diagnostic_case(case, tmp_path))

    assert diagnostics, f"Expected diagnostics for {case.filename}"
    assert any(d.is_error for d in diagnostics)

    messages = "\n".join(d.message for d in diagnostics)
    assert any(expected in messages for expected in case.expected_messages), messages


@pytest.mark.parametrize(
    "language",
    STARTUP_ONLY_LANGUAGES,
    ids=get_language_id_string,
)
def test_installed_lsp_starts_without_diagnostics_assertion(
    language: LanguageId,
    tmp_path: Path,
) -> None:
    """Smoke-test installed LSPs that do not reliably publish diagnostics.

    bash-language-server starts successfully, but it does not behave like
    shellcheck and may not publish syntax diagnostics for simple broken files.
    """
    asyncio.run(_run_startup_smoke(language, tmp_path))
