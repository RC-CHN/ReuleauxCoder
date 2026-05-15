"""Opt-in integration smoke tests for the active LSP tool.

Creates temporary source files, starts real LSP servers, and exercises
the unified ``lsp`` tool with goToDefinition / findReferences /
documentSymbol operations.

Run with:
    RCODER_RUN_LSP_INTEGRATION=1 uv run python -m pytest \\
        tests/extensions/tools/test_lsp_tool_integration.py -q -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId
from reuleauxcoder.extensions.tools.builtin.lsp import LspTool, set_lsp_manager

RUN_LSP_INTEGRATION = os.environ.get("RCODER_RUN_LSP_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_LSP_INTEGRATION,
    reason="Set RCODER_RUN_LSP_INTEGRATION=1 to run real LSP smoke tests.",
)

# ── helpers ───────────────────────────────────────────────────────────────


def _setup_manager(tmp_path: Path, lang: LanguageId) -> LspManager:
    """Start an LspManager with the given language enabled and inject it
    into the LSP tool module."""
    mgr = LspManager(
        LspConfig(enabled=True, poll_timeout_ms=8000, max_diagnostics=20),
        workspace_cwd=tmp_path,
    )
    mgr._availability[lang] = True
    mgr.start_worker()
    set_lsp_manager(mgr)
    return mgr


def _teardown_manager(mgr: LspManager) -> None:
    """Shut down the manager and clear the tool module reference."""
    mgr.shutdown_all()
    set_lsp_manager(None)


# ── tests ─────────────────────────────────────────────────────────────────


class TestLspToolIntegration:
    def test_go_to_definition_python(self, tmp_path: Path) -> None:
        """Find where a function called in one file is defined in another."""
        src = tmp_path / "src"
        src.mkdir()
        lib = src / "lib.py"
        lib.write_text(
            "def greet(name: str) -> str:\n"
            '    return f"Hello, {name}"\n'
        )
        main = src / "main.py"
        main.write_text(
            "from lib import greet\n\n"
            'result = greet("World")\n'
        )

        mgr = _setup_manager(tmp_path, LanguageId.PYTHON)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="goToDefinition",
                filePath=str(main),
                line=3,
                character=14,  # cursor on "greet"
            )
            assert "lib.py" in out, out
            assert "Defined in" in out or "definitions" in out, out
        finally:
            _teardown_manager(mgr)

    def test_find_references_python(self, tmp_path: Path) -> None:
        """Find all calls to a function across files."""
        src = tmp_path / "src"
        src.mkdir()
        lib = src / "lib.py"
        lib.write_text(
            "def greet(name: str) -> str:\n"
            '    return f"Hello, {name}"\n'
        )
        main = src / "main.py"
        main.write_text(
            "from lib import greet\n\n"
            'result = greet("World")\n'
        )

        mgr = _setup_manager(tmp_path, LanguageId.PYTHON)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="findReferences",
                filePath=str(lib),
                line=1,
                character=5,  # cursor on "greet" definition
            )
            # Should find at least the call in main.py
            assert "reference" in out.lower(), out
        finally:
            _teardown_manager(mgr)

    def test_document_symbol_python(self, tmp_path: Path) -> None:
        """List symbols in a Python file."""
        f = tmp_path / "example.py"
        f.write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n\n"
            "def main() -> None:\n"
            '    print(add(1, 2))\n'
        )

        mgr = _setup_manager(tmp_path, LanguageId.PYTHON)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="documentSymbol",
                filePath=str(f),
                line=1,
                character=1,
            )
            assert "symbol" in out.lower(), out
            assert "[function] add" in out, out
            assert "[function] main" in out, out
        finally:
            _teardown_manager(mgr)

    def test_go_to_definition_typescript(self, tmp_path: Path) -> None:
        """Find where a TS function is defined (smoke test with tsserver)."""
        f = tmp_path / "test.ts"
        f.write_text(
            "function add(a: number, b: number): number {\n"
            "    return a + b\n"
            "}\n\n"
            "const x = add(1, 2)\n"
        )

        mgr = _setup_manager(tmp_path, LanguageId.TYPESCRIPT)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="goToDefinition",
                filePath=str(f),
                line=5,
                character=13,  # cursor on "add" in the call
            )
            assert "Defined in" in out, out
        finally:
            _teardown_manager(mgr)

    def test_document_symbol_go(self, tmp_path: Path) -> None:
        """List symbols in a Go file (requires gopls)."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text("module test\n\ngo 1.21\n")
        f = tmp_path / "main.go"
        f.write_text(
            "package main\n\n"
            "import \"fmt\"\n\n"
            "func greet(name string) string {\n"
            '    return fmt.Sprintf("Hello, %s", name)\n'
            "}\n\n"
            "func main() {\n"
            '    greet("World")\n'
            "}\n"
        )

        mgr = _setup_manager(tmp_path, LanguageId.GO)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="documentSymbol",
                filePath=str(f),
                line=1,
                character=1,
            )
            assert "symbol" in out.lower(), out
            assert "[function] greet" in out, out
            assert "[function] main" in out, out
        finally:
            _teardown_manager(mgr)

    def test_document_symbol_c(self, tmp_path: Path) -> None:
        """List symbols in a C file (requires clangd)."""
        f = tmp_path / "test.c"
        f.write_text(
            "#include <stdio.h>\n\n"
            "int add(int a, int b) {\n"
            "    return a + b;\n"
            "}\n\n"
            "int main(void) {\n"
            '    printf("%d\\n", add(1, 2));\n'
            "    return 0;\n"
            "}\n"
        )

        mgr = _setup_manager(tmp_path, LanguageId.C)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="documentSymbol",
                filePath=str(f),
                line=1,
                character=1,
            )
            assert "symbol" in out.lower(), out
            assert "[function] add" in out, out
            assert "[function] main" in out, out
        finally:
            _teardown_manager(mgr)

    def test_unsupported_operation(self, tmp_path: Path) -> None:
        """Unknown operation returns a helpful error."""
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")

        mgr = _setup_manager(tmp_path, LanguageId.PYTHON)
        try:
            tool = LspTool()
            out = tool.execute(
                operation="hover",  # not in our enum
                filePath=str(f),
                line=1,
                character=1,
            )
            assert "Unknown operation" in out, out
            assert "goToDefinition" in out, out
        finally:
            _teardown_manager(mgr)

    def test_missing_file(self) -> None:
        """Missing file returns a clear error (no LSP needed)."""
        tool = LspTool()
        out = tool.execute(
            operation="goToDefinition",
            filePath="/nonexistent/file.py",
            line=1,
            character=1,
        )
        assert "not found" in out, out
