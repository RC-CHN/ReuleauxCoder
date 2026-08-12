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

from reuleauxcoder.extensions.lsp.client import LspClient, LspClientError
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.domain.hooks.builtin.lsp_edit_observer import LspEditObserverHook
from reuleauxcoder.domain.hooks.builtin.lsp_injector import (
    LspDiagnosticsInjectorHook,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    get_language_id_string,
    resolve_server_launch,
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
    setup_files: dict[str, str] | None = None
    diagnostic_timeout: float = 20.0
    # ^ extra files to write into tmp_path before starting the LSP
    #   (e.g. go.mod for gopls).  All are cleaned up with tmp_path.


DIAGNOSTIC_CASES: tuple[DiagnosticCase, ...] = (
    DiagnosticCase(
        language=LanguageId.PYTHON,
        filename="broken.py",
        content=(
            "def greet(name: str) -> str:\n"
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
    DiagnosticCase(
        language=LanguageId.BASH,
        filename="broken.sh",
        content="if [ -f /tmp/nope ]; then\n  echo yes\n",
        expected_messages=("Couldn't find 'fi'",),
    ),
    DiagnosticCase(
        language=LanguageId.GO,
        filename="broken.go",
        content='package main\n\nfunc main() {\n    x := "hello"\n}\n',
        expected_messages=("declared and not used",),
        setup_files={
            "go.mod": "module test\n\ngo 1.21\n",
        },
    ),
    DiagnosticCase(
        language=LanguageId.C,
        filename="broken.c",
        content='#include <stdio.h>\n\nint main() {\n    int x = "oops";\n    return 0\n}\n',
        expected_messages=("Incompatible pointer to integer conversion",),
    ),
    DiagnosticCase(
        language=LanguageId.CPP,
        filename="broken.cpp",
        content="class Foo {\n    int x\n};\n",
        expected_messages=("Expected ';' at end of declaration list",),
    ),
    DiagnosticCase(
        language=LanguageId.RUST,
        filename="src/main.rs",
        content='fn main() {\n    let x: i32 = "oops";\n}\n',
        expected_messages=("mismatched types", "expected i32"),
        setup_files={
            "Cargo.toml": '[package]\nname = "test"\nversion = "0.1.0"\nedition = "2021"\n',
        },
        diagnostic_timeout=60.0,
    ),
)


STARTUP_ONLY_LANGUAGES: tuple[LanguageId, ...] = ()


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


async def _wait_for_fresh_publish(
    client: LspClient,
    file_path: Path,
    *,
    after_generation: int,
    timeout: float = 20.0,
) -> list[Diagnostic]:
    """Wait for a new publish, preserving the distinction between clean and timeout."""
    diagnostics = await client.wait_for_diagnostics(
        file_path,
        timeout=timeout,
        after_generation=after_generation,
    )
    assert client.diagnostics_generation(file_path) > after_generation, (
        f"No fresh diagnostics publish for {file_path} after generation "
        f"{after_generation}"
    )
    return diagnostics


async def _start_python_client(root: Path) -> LspClient:
    cmd, args = get_server_command(LanguageId.PYTHON)
    if shutil.which(cmd) is None:
        pytest.skip(f"{cmd} is not available on PATH")
    client = LspClient(language_id=LanguageId.PYTHON, workspace_root=root)
    await asyncio.wait_for(client.spawn(cmd, args), timeout=30.0)
    await asyncio.wait_for(client.initialize(), timeout=30.0)
    return client


async def _run_diagnostic_case(
    case: DiagnosticCase,
    tmp_path: Path,
    *,
    typescript_mode: str = "auto",
) -> list[Diagnostic]:
    launch = resolve_server_launch(
        case.language, tmp_path, typescript_mode=typescript_mode
    )
    cmd, args = launch.command, list(launch.args)
    if shutil.which(cmd) is None:
        pytest.skip(f"{cmd} is not available on PATH")

    # Write any auxiliary files the language server needs
    if case.setup_files:
        for name, text in case.setup_files.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    file_path = tmp_path / case.filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(case.content, encoding="utf-8")

    client = LspClient(language_id=case.language, workspace_root=tmp_path)
    try:
        await asyncio.wait_for(client.spawn(cmd, args), timeout=30.0)
        try:
            await asyncio.wait_for(
                client.initialize(launch.initialization_options),
                timeout=30.0,
            )
        except LspClientError as exc:
            if case.language in {
                LanguageId.TYPESCRIPT,
                LanguageId.JAVASCRIPT,
            } and "valid TypeScript installation" in str(exc):
                pytest.skip(
                    "typescript-language-server requires a TypeScript installation "
                    "in the temporary workspace or an explicit tsserver.path"
                )
            raise
        await client.did_open(file_path, case.content)
        # Native TypeScript 7 advertises pull diagnostics instead of sending
        # publishDiagnostics notifications.  Exercise the same refresh path as
        # the runtime; this remains a no-op for push-only language servers.
        await client.refresh_diagnostics(file_path)
        return await _collect_non_empty_diagnostics(
            client,
            file_path,
            timeout=case.diagnostic_timeout,
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
    "case",
    tuple(
        case
        for case in DIAGNOSTIC_CASES
        if case.language in {LanguageId.TYPESCRIPT, LanguageId.JAVASCRIPT}
    ),
    ids=lambda case: f"legacy-{get_language_id_string(case.language)}",
)
def test_typescript_legacy_v6_adapter_reports_diagnostics(
    case: DiagnosticCase,
    tmp_path: Path,
) -> None:
    diagnostics = asyncio.run(
        _run_diagnostic_case(case, tmp_path, typescript_mode="legacy")
    )

    assert diagnostics
    assert any(item.is_error for item in diagnostics)


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

    Currently empty — all installed language servers have diagnostics assertions.
    Add languages here when a new LSP is installed but diagnostics are unreliable.
    """
    asyncio.run(_run_startup_smoke(language, tmp_path))


def test_python_broken_fixed_broken_and_rapid_save_sequence(
    tmp_path: Path,
) -> None:
    """A real server must publish clean explicitly and finish on the newest edit."""

    async def run() -> None:
        path = tmp_path / "sequence.py"
        broken_one = "def first(:\n    pass\n"
        fixed = "def first():\n    pass\n"
        broken_two = "def second(:\n    pass\n"
        path.write_text(broken_one, encoding="utf-8")
        client = await _start_python_client(tmp_path)
        try:
            await client.did_open(path, broken_one)
            first = await _wait_for_fresh_publish(client, path, after_generation=0)
            assert first

            baseline = client.diagnostics_generation(path)
            path.write_text(fixed, encoding="utf-8")
            await client.did_change(path, fixed)
            clean = await _wait_for_fresh_publish(
                client, path, after_generation=baseline
            )
            assert clean == []

            baseline = client.diagnostics_generation(path)
            path.write_text(broken_two, encoding="utf-8")
            await client.did_change(path, broken_two)
            second = await _wait_for_fresh_publish(
                client, path, after_generation=baseline
            )
            assert second

            # Three saves without waiting may be coalesced by the server.  The
            # client version must still advance for every save and the final
            # publish must describe the newest (broken) document.
            baseline = client.diagnostics_generation(path)
            rapid_versions = (
                "value = 1\n",
                "value = 2\n",
                "def newest(:\n    pass\n",
            )
            for content in rapid_versions:
                path.write_text(content, encoding="utf-8")
                await client.did_change(path, content)
            latest = await _wait_for_fresh_publish(
                client, path, after_generation=baseline
            )
            assert latest
            assert client.document_version(path) == 6
        finally:
            await asyncio.wait_for(client.shutdown(), timeout=10.0)

    asyncio.run(run())


def test_python_two_documents_publish_independently(tmp_path: Path) -> None:
    async def run() -> None:
        first_path = tmp_path / "first.py"
        second_path = tmp_path / "second.py"
        first_text = "def first(:\n    pass\n"
        second_text = "def second(:\n    pass\n"
        first_path.write_text(first_text, encoding="utf-8")
        second_path.write_text(second_text, encoding="utf-8")
        client = await _start_python_client(tmp_path)
        try:
            await client.did_open(first_path, first_text)
            await client.did_open(second_path, second_text)
            first, second = await asyncio.gather(
                _collect_non_empty_diagnostics(client, first_path),
                _collect_non_empty_diagnostics(client, second_path),
            )
            assert first
            assert second
            assert client.diagnostics_generation(first_path) >= 1
            assert client.diagnostics_generation(second_path) >= 1
        finally:
            await asyncio.wait_for(client.shutdown(), timeout=10.0)

    asyncio.run(run())


def test_python_workspace_roots_use_independent_real_transports(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        root_a = tmp_path / "root-a"
        root_b = tmp_path / "root-b"
        root_a.mkdir()
        root_b.mkdir()
        path_a = root_a / "main.py"
        path_b = root_b / "main.py"
        text = "def broken(:\n    pass\n"
        path_a.write_text(text, encoding="utf-8")
        path_b.write_text(text, encoding="utf-8")
        client_a, client_b = await asyncio.gather(
            _start_python_client(root_a),
            _start_python_client(root_b),
        )
        try:
            await asyncio.gather(
                client_a.did_open(path_a, text),
                client_b.did_open(path_b, text),
            )
            diagnostics_a, diagnostics_b = await asyncio.gather(
                _wait_for_fresh_publish(client_a, path_a, after_generation=0),
                _wait_for_fresh_publish(client_b, path_b, after_generation=0),
            )
            assert diagnostics_a
            assert diagnostics_b
            assert client_a.document_version(path_b) == 0
            assert client_b.document_version(path_a) == 0
        finally:
            await asyncio.gather(
                client_a.shutdown(),
                client_b.shutdown(),
            )

    asyncio.run(run())


def test_python_parent_transport_remains_owned_while_subagent_scope_is_omitted(
    tmp_path: Path,
) -> None:
    """The explicit subagent policy is omission, never sharing parent transport."""
    path = tmp_path / "parent.py"
    content = "def parent(:\n    pass\n"
    path.write_text(content, encoding="utf-8")
    manager = LspManager(
        LspConfig(enabled=True, poll_timeout_ms=20_000),
        workspace_cwd=tmp_path,
    )
    report = manager.health_check()
    if not any(
        name == "python" and available for name, available, _details in report.languages
    ):
        pytest.skip("Python language server is not available")
    registry = HookRegistry()
    registry.register(
        HookPoint.AFTER_TOOL_EXECUTE,
        LspEditObserverHook(lsp_manager=manager),
    )
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        LspDiagnosticsInjectorHook(lsp_manager=manager),
    )
    manager.start_worker()
    try:
        batch_id = manager.enqueue_diagnostics(
            path,
            route=DiagnosticRoute(
                file_path=path,
                agent_id="parent",
                session_generation=0,
                session_id="session",
                turn_id="turn",
                tool_call_id="tool",
            ),
        )
        assert batch_id is not None

        child_registry = registry.clone(scope="subagent")
        child_hooks = (
            *child_registry.hooks_at(HookPoint.AFTER_TOOL_EXECUTE),
            *child_registry.hooks_at(HookPoint.BEFORE_LLM_REQUEST),
        )
        assert child_hooks
        assert all(getattr(hook, "lsp_manager", None) is None for hook in child_hooks)

        deadline = time.monotonic() + 30
        batches = ()
        while time.monotonic() < deadline and not batches:
            batches = manager.pending_diagnostic_batches(batch_id=batch_id)
            if not batches:
                time.sleep(0.1)
        assert len(batches) == 1
        assert batches[0].block.items
        assert batches[0].route.agent_id == "parent"

        manager.advance_session_generation("parent", 1)
        assert manager.pending_diagnostic_batches() == ()
    finally:
        manager.shutdown_all()

    assert manager._worker_thread is None
    assert manager._transports == {}


def test_python_document_commit_produces_real_diagnostics_batch(
    tmp_path: Path,
) -> None:
    """Exercise the ordered sync -> didSave -> diagnostics manager path."""

    path = tmp_path / "committed.py"
    path.write_text("def committed(:\n    pass\n", encoding="utf-8")
    manager = LspManager(
        LspConfig(enabled=True, poll_timeout_ms=20_000),
        workspace_cwd=tmp_path,
    )
    report = manager.health_check()
    if not any(
        name == "python" and available for name, available, _details in report.languages
    ):
        pytest.skip("Python language server is not available")

    manager.start_worker()
    try:
        batch_id = manager.enqueue_diagnostics(
            path,
            route=DiagnosticRoute(
                file_path=path,
                agent_id="parent",
                session_generation=0,
                session_id="session",
                turn_id="turn",
                tool_call_id="edit",
            ),
            document_committed=True,
        )
        assert batch_id is not None

        deadline = time.monotonic() + 30
        batches = ()
        while time.monotonic() < deadline and not batches:
            batches = manager.pending_diagnostic_batches(batch_id=batch_id)
            if not batches:
                time.sleep(0.1)

        assert len(batches) == 1
        assert batches[0].block.items
        assert batches[0].route.tool_call_id == "edit"

        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            messages=[
                {
                    "role": "user",
                    "content": (
                        '<execution_state plan_revision="0">\n'
                        '<execution_data trust="untrusted_data">\n{}\n'
                        "</execution_data>\n"
                        "<runtime_instruction>Continue.</runtime_instruction>\n"
                        "</execution_state>"
                    ),
                }
            ],
            agent_id="parent",
            session_generation=0,
            session_id="session",
            turn_id="next-turn",
        )
        LspDiagnosticsInjectorHook(lsp_manager=manager).run(context)

        assert "[LSP DIAGNOSTICS]" in context.messages[0]["content"]
        assert [batch.batch_id for batch in manager.pending_diagnostic_batches()] == [
            batch_id
        ]
        assert manager.diagnostic_batch_acknowledgement(batch_id) is None

        assert context._commit_dispatch_callbacks() == ()
        assert manager.pending_diagnostic_batches() == ()
        assert manager.diagnostic_batch_acknowledgement(batch_id) == (
            "lsp-inject:parent:0:next-turn"
        )
    finally:
        manager.shutdown_all()

    assert manager._worker_thread is None
    assert manager._transports == {}
