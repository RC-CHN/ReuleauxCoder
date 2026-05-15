"""Unit tests for active LSP tool helpers.

Tests resolve_file_path, validate_position, format_location,
format_locations, format_references, and format_document_symbols
without requiring a real LSP server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from reuleauxcoder.extensions.lsp.registry import LanguageId
from reuleauxcoder.extensions.lsp.tool_helpers import (
    MAX_REFERENCES,
    format_document_symbols,
    format_location,
    format_locations,
    format_references,
    resolve_file_path,
    validate_position,
)


# ── resolve_file_path ─────────────────────────────────────────────────────


class TestResolveFilePath:
    def test_existing_python_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("x = 1\n")
        lang, path = resolve_file_path(str(f))
        assert lang == LanguageId.PYTHON
        assert path == f.resolve()

    def test_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            resolve_file_path("/nonexistent/file.xyz")

    def test_directory_not_file(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a file"):
            resolve_file_path(str(tmp_path))

    def test_unsupported_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "data.bin"
        f.write_text("hello")
        with pytest.raises(ValueError, match=r"No LSP server available"):
            resolve_file_path(str(f))


# ── validate_position ─────────────────────────────────────────────────────


class TestValidatePosition:
    def test_valid_position(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        total = validate_position(f, 2, 1)
        assert total == 4  # 3 newlines + trailing content = 4 lines

    def test_beyond_end(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("only one line\n")
        with pytest.raises(ValueError, match="beyond end of file"):
            validate_position(f, 99, 1)

    def test_line_less_than_one(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("x\n")
        with pytest.raises(ValueError, match="Line number must be >= 1"):
            validate_position(f, 0, 1)

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("")
        # Empty file has 0 lines — any position is beyond end
        with pytest.raises(ValueError, match="beyond end of file"):
            validate_position(f, 1, 1)


# ── format_location ───────────────────────────────────────────────────────


class TestFormatLocation:
    def test_location_with_file_uri(self) -> None:
        loc = {
            "uri": "file:///home/user/src/main.py",
            "range": {"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 10}},
        }
        result = format_location(loc)
        assert result is not None
        assert ":10:5" in result  # 0-based → 1-based

    def test_locationlink_with_target_uri(self) -> None:
        loc = {
            "targetUri": "file:///home/user/src/main.py",
            "targetRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
        }
        result = format_location(loc)
        assert result is not None
        assert ":1:1" in result

    def test_missing_uri_returns_none(self) -> None:
        loc = {"range": {"start": {"line": 1, "character": 2}}}
        assert format_location(loc) is None

    def test_missing_range_returns_none(self) -> None:
        loc = {"uri": "file:///x.py"}
        assert format_location(loc) is None

    def test_with_prefix(self) -> None:
        loc = {
            "uri": "file:///src/a.py",
            "range": {"start": {"line": 4, "character": 2}},
        }
        result = format_location(loc, prefix="  ")
        assert result is not None
        assert result.startswith("  ")


# ── format_locations ──────────────────────────────────────────────────────


class TestFormatLocations:
    def test_single_definition(self) -> None:
        raw = {
            "uri": "file:///src/core.py",
            "range": {"start": {"line": 14, "character": 4}},
        }
        out = format_locations(raw, file_path="/tmp/test.py")
        assert "Defined in" in out
        assert "src/core.py" in out

    def test_multiple_definitions(self) -> None:
        raw = [
            {
                "uri": "file:///src/base.py",
                "range": {"start": {"line": 9, "character": 2}},
            },
            {
                "uri": "file:///src/impl.py",
                "range": {"start": {"line": 4, "character": 4}},
            },
        ]
        out = format_locations(raw, file_path="/tmp/test.py")
        assert "Found 2 definitions" in out
        assert "src/base.py" in out
        assert "src/impl.py" in out

    def test_null_returns_no_definition(self) -> None:
        out = format_locations(None, file_path="/tmp/test.py")
        assert "No definition found" in out

    def test_empty_list_returns_no_definition(self) -> None:
        out = format_locations([], file_path="/tmp/test.py")
        assert "No definition found" in out


# ── format_references ─────────────────────────────────────────────────────


class TestFormatReferences:
    def test_single_reference(self) -> None:
        raw = [
            {
                "uri": "file:///src/caller.py",
                "range": {"start": {"line": 5, "character": 10}},
            },
        ]
        out = format_references(raw, file_path="/tmp/test.py")
        assert "1 reference" in out
        assert "caller.py" in out
        assert "Line 6:11" in out  # 1-based

    def test_multiple_files(self) -> None:
        raw = [
            {
                "uri": "file:///src/a.py",
                "range": {"start": {"line": 0, "character": 0}},
            },
            {
                "uri": "file:///src/b.py",
                "range": {"start": {"line": 1, "character": 2}},
            },
        ]
        out = format_references(raw, file_path="/tmp/test.py")
        assert "2 references" in out
        assert "across 2 files" in out

    def test_many_references_truncated(self) -> None:
        raw = []
        for i in range(MAX_REFERENCES + 10):
            raw.append({
                "uri": "file:///src/x.py",
                "range": {"start": {"line": i, "character": 0}},
            })
        out = format_references(raw, file_path="/tmp/test.py")
        assert f"{MAX_REFERENCES + 10} references" in out
        assert "not shown" in out

    def test_no_references(self) -> None:
        out = format_references(None, file_path="/tmp/test.py")
        assert "No references found" in out


# ── format_document_symbols ───────────────────────────────────────────────


class TestFormatDocumentSymbols:
    def test_hierarchical_symbols(self) -> None:
        raw = [
            {
                "name": "RequestHandler",
                "kind": 5,  # class
                "range": {"start": {"line": 9, "character": 0}},
                "children": [
                    {
                        "name": "__init__",
                        "kind": 6,  # method
                        "range": {"start": {"line": 14, "character": 4}},
                    },
                ],
            },
        ]
        out = format_document_symbols(raw, file_path="/tmp/test.py")
        assert "2 symbols" in out
        assert "[class] RequestHandler" in out
        assert "[method] __init__" in out

    def test_flat_symbol_information(self) -> None:
        raw = [
            {
                "name": "main",
                "kind": 12,  # function
                "location": {"range": {"start": {"line": 20, "character": 0}}},
            },
        ]
        out = format_document_symbols(raw, file_path="/tmp/test.py")
        assert "1 symbol" in out
        assert "[function] main" in out

    def test_empty_returns_no_symbols(self) -> None:
        out = format_document_symbols(None, file_path="/tmp/test.py")
        assert "No symbols found" in out

    def test_deeply_nested(self) -> None:
        raw = [
            {
                "name": "A",
                "kind": 5,
                "range": {"start": {"line": 0, "character": 0}},
                "children": [
                    {
                        "name": "B",
                        "kind": 6,
                        "range": {"start": {"line": 1, "character": 2}},
                        "children": [
                            {
                                "name": "C",
                                "kind": 12,  # function inside method
                                "range": {"start": {"line": 2, "character": 4}},
                            },
                        ],
                    },
                ],
            },
        ]
        out = format_document_symbols(raw, file_path="/tmp/test.py")
        assert "3 symbols" in out
        # Each deeper level gets more indentation
        lines = out.split("\n")
        assert "  [class] A" in lines[1]
        assert "    [method] B" in lines[2]
        assert "      [function] C" in lines[3]
