"""Active LSP tools — goToDefinition, findReferences, documentSymbol.

A single ``lsp`` tool dispatches on *operation*; all operations share the
same input shape (filePath / line / character).  The real LSP requests are
sent through ``LspManager.send_request_sync()`` which bridges to the worker
thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from reuleauxcoder.extensions.lsp.client import LspClientError
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.tool_helpers import (
    format_document_symbols,
    format_locations,
    format_references,
    resolve_file_path,
    validate_position,
)
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.registry import register_tool

# ── helpers ───────────────────────────────────────────────────────────────

_OPERATIONS = frozenset({"goToDefinition", "findReferences", "documentSymbol"})

_lsp_manager: LspManager | None = None


def set_lsp_manager(mgr: LspManager | None) -> None:
    """Called by the app runner once the LSP infrastructure is ready."""
    global _lsp_manager
    _lsp_manager = mgr


def _get_lsp_manager() -> LspManager | None:
    """Return the singleton LspManager if the LSP infrastructure is active."""
    return _lsp_manager


# ── tool ───────────────────────────────────────────────────────────────────


@register_tool
class LspTool(Tool):
    """Single tool that dispatches LSP operations.

    Supported operations:
    - goToDefinition: Find where a symbol at filePath:line:character is defined
    - findReferences: Find all references to the symbol at the position
    - documentSymbol: List all symbols (classes, methods, etc.) in the file
    """

    name: ClassVar[str] = "lsp"
    description: ClassVar[str] = (
        "Interact with Language Server Protocol (LSP) servers for code intelligence.\n"
        "\n"
        "Supported operations:\n"
        "- goToDefinition: Find where a symbol is defined\n"
        "- findReferences: Find all references to a symbol across the codebase\n"
        "- documentSymbol: Get all symbols (functions, classes, variables) in a file\n"
        "\n"
        "All operations require:\n"
        "- filePath: The absolute or relative path to the file\n"
        "- line: The line number (1-based, as shown in editors)\n"
        "- character: The character offset (1-based, as shown in editors)\n"
        "\n"
        "Note: An LSP server must be available for the file type. "
        "If no server is running, an error will be returned."
    )

    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["goToDefinition", "findReferences", "documentSymbol"],
                "description": "The LSP operation to perform.",
            },
            "filePath": {
                "type": "string",
                "description": "The absolute or relative path to the file.",
            },
            "line": {
                "type": "integer",
                "description": "The line number (1-based, as shown in editors).",
            },
            "character": {
                "type": "integer",
                "description": "The character offset (1-based, as shown in editors).",
            },
        },
        "required": ["operation", "filePath", "line", "character"],
    }

    def execute(
        self,
        *,
        operation: str,
        filePath: str,
        line: int,
        character: int,
    ) -> str:
        # 1. Validate operation
        if operation not in _OPERATIONS:
            return (
                f"Unknown operation: {operation}. "
                f"Supported: {', '.join(sorted(_OPERATIONS))}."
            )

        # 2. Resolve file and language
        try:
            lang, path = resolve_file_path(filePath)
        except FileNotFoundError as e:
            return str(e)
        except ValueError as e:
            return str(e)

        # 3. Position validation (skip for documentSymbol — line/char are
        #    ignored by the server anyway, they only exist to keep the schema
        #    uniform)
        if operation != "documentSymbol":
            try:
                validate_position(path, line, character)
            except ValueError as e:
                return str(e)

        # 4. Get LSP manager
        manager = _get_lsp_manager()
        if manager is None:
            return "LSP infrastructure is not available"

        # 5. Build LSP method + params
        if operation == "goToDefinition":
            method = "textDocument/definition"
            params = _position_params(path, line, character)
        elif operation == "findReferences":
            method = "textDocument/references"
            params = {
                **_position_params(path, line, character),
                "context": {"includeDeclaration": True},
            }
        elif operation == "documentSymbol":
            method = "textDocument/documentSymbol"
            params = {"textDocument": {"uri": path.resolve().as_uri()}}
        else:
            return f"Unknown operation: {operation}"

        # 6. Send request through worker thread
        try:
            raw = manager.send_request_sync(path, method, params)
        except LspClientError as e:
            return f"LSP server for this file type is not responding: {e}"
        except Exception as e:
            return f"LSP request failed: {e}"

        # 7. Format result
        try:
            if operation == "goToDefinition":
                return format_locations(raw, file_path=str(path))
            if operation == "findReferences":
                return format_references(raw, file_path=str(path))
            if operation == "documentSymbol":
                return format_document_symbols(raw, file_path=str(path))
        except Exception as e:
            return f"Failed to format LSP result: {e}"

        return f"Unknown operation: {operation}"


# ── internal helpers ───────────────────────────────────────────────────────


def _position_params(path: Path, line: int, character: int) -> dict[str, Any]:
    """Build ``textDocument`` + ``position`` params for position-based LSP methods."""
    return {
        "textDocument": {"uri": path.resolve().as_uri()},
        "position": {
            "line": line - 1,  # 1-based → 0-based
            "character": character - 1,
        },
    }
