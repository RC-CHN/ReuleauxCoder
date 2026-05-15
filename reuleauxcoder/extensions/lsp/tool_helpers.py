"""Shared helpers for active LSP tools.

Resolves file paths, validates positions, and formats LSP results into
human-readable output suitable for LLM consumption.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    detect_language,
    get_language_id_string,
)

# Maximum references to display before truncating
MAX_REFERENCES = 50

# SymbolKind → label mapping (LSP spec)
_SYMBOL_KIND_LABELS: dict[int, str] = {
    1: "file",
    2: "module",
    3: "namespace",
    4: "package",
    5: "class",
    6: "method",
    7: "property",
    8: "field",
    9: "constructor",
    10: "enum",
    11: "interface",
    12: "function",
    13: "variable",
    14: "constant",
    15: "string",
    16: "number",
    17: "boolean",
    18: "array",
    19: "object",
    20: "key",
    21: "null",
    22: "enum-member",
    23: "struct",
    24: "event",
    25: "operator",
    26: "type-parameter",
}


def resolve_file_path(file_path: str) -> tuple[LanguageId, Path]:
    """Validate file existence and detect its language.

    Returns (language, resolved_path) or raises FileNotFoundError /
    ValueError with a human-readable message.
    """
    path = Path(file_path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"{file_path} not found")

    if not path.is_file():
        raise ValueError(f"{file_path} is not a file")

    lang = detect_language(path)
    if lang is None:
        raise ValueError(
            f"No LSP server available for \"{path.suffix}\" files"
        )

    return lang, path


def validate_position(file_path: Path, line: int, character: int) -> int:
    """Check that line/character are within the file.

    Returns total line count on success or raises ValueError.
    """
    total = 0
    try:
        text = file_path.read_text(encoding="utf-8")
        total = text.count("\n") + (1 if len(text) > 0 else 0)
    except Exception:
        # can't read file → skip validation, let the LSP deal with it
        return 0

    if line > total:
        raise ValueError(
            f"Position {line}:{character} is beyond end of file "
            f"({total} line{'s' if total != 1 else ''})"
        )

    # LSP uses 0-based internally; we expose 1-based to the model
    if line < 1:
        raise ValueError(f"Line number must be >= 1, got {line}")

    return total


def format_location(
    location: dict[str, Any],
    *,
    prefix: str = "",
) -> str | None:
    """Format a single Location / LocationLink into a human-readable string.

    Returns ``"file:line:character"`` or ``None`` if the location is
    malformed (missing uri/range).
    """
    uri = location.get("uri") or location.get("targetUri")
    if not uri:
        return None

    rng = location.get("range") or location.get("targetSelectionRange") or location.get("targetRange")
    if not rng:
        return None

    start = rng.get("start", {})

    # Convert file:// URI to a path
    path = _uri_to_path(uri)

    line = start.get("line", 0) + 1       # 0-based → 1-based
    char = start.get("character", 0) + 1

    return f"{prefix}{path}:{line}:{char}"


def format_locations(
    raw: Any,
    *,
    file_path: str,
) -> str:
    """Format a list of Location / LocationLink into readable output.

    Handles:
    - ``null`` / empty → "No definition found"
    - single location
    - multiple locations (e.g. overloaded functions)
    """
    locations: list[dict[str, Any]] = []
    if raw is None:
        pass
    elif isinstance(raw, list):
        locations = raw
    elif isinstance(raw, dict):
        locations = [raw]

    if not locations:
        return f"No definition found for the symbol at {file_path}"

    # Normalise location → destination info
    formatted: list[str] = []
    for i, loc in enumerate(locations):
        label = f"  " if len(locations) > 1 else ""
        loc_str = format_location(loc, prefix=label)
        if loc_str:
            formatted.append(loc_str)

    if not formatted:
        return f"No definition found for the symbol at {file_path}"

    if len(formatted) == 1:
        return f"Defined in {formatted[0].strip()}"

    return f"Found {len(formatted)} definitions:\n" + "\n".join(formatted)


def format_references(
    raw: Any,
    *,
    file_path: str,
) -> str:
    """Format findReferences results grouped by file.

    Truncates at ``MAX_REFERENCES`` entries.
    """
    locations: list[dict[str, Any]] = []
    if isinstance(raw, list):
        locations = raw
    elif isinstance(raw, dict) and "uri" in raw:
        locations = [raw]

    if not locations:
        return f"No references found for the symbol at {file_path}"

    # Group by file
    by_file: dict[str, list[dict[str, Any]]] = {}
    for loc in locations:
        uri = loc.get("uri") or loc.get("targetUri")
        if not uri:
            continue
        path = _uri_to_path(uri)
        by_file.setdefault(path, []).append(loc)

    if not by_file:
        return f"No references found for the symbol at {file_path}"

    # Count total
    total = sum(len(v) for v in by_file.values())
    truncated = total > MAX_REFERENCES

    lines: list[str] = []
    shown = 0
    for p in sorted(by_file.keys()):
        locs = by_file[p]
        lines.append(f"{p}:")
        for loc in locs:
            if truncated and shown >= MAX_REFERENCES:
                break
            rng = loc.get("range")
            if not rng:
                continue
            line = rng.get("start", {}).get("line", 0) + 1
            char = rng.get("start", {}).get("character", 0) + 1
            lines.append(f"  Line {line}:{char}")
            shown += 1
        if truncated and shown >= MAX_REFERENCES:
            break

    header = f"Found {total} reference{'s' if total != 1 else ''}"
    if len(by_file) > 1:
        header += f" across {len(by_file)} files"
    header += ":"

    if truncated:
        lines.append(
            f"... [{total - MAX_REFERENCES} more reference(s) not shown; "
            f"narrow the search or check individual files]"
        )

    return "\n".join([header, *lines])


def format_document_symbols(
    raw: Any,
    *,
    file_path: str,
) -> str:
    """Format DocumentSymbol[] or SymbolInformation[] into a hierarchical tree.

    ``DocumentSymbol`` has ``children`` for nested scopes (class → method).
    ``SymbolInformation`` is a flat list; we render it as-is and note the
    format.
    """
    if not raw or not isinstance(raw, list) or len(raw) == 0:
        return f"No symbols found in {file_path}"

    # Detect format: DocumentSymbol has "kind" + optional "children"
    #               SymbolInformation has "kind" + "location"
    first = raw[0]
    if "location" in first and "kind" in first:
        # SymbolInformation[] — flat list
        formatted = _render_flat_symbols(raw, file_path)
    else:
        # DocumentSymbol[] — hierarchical
        formatted = _render_hierarchical_symbols(raw, file_path)

    total = _count_symbols(raw)
    header = f"{total} symbol{'s' if total != 1 else ''} in {file_path}:"
    return header + "\n" + formatted


# ── Internal helpers ──────────────────────────────────────────────────────


def _uri_to_path(uri: str) -> str:
    """Convert file:// URI to a path string."""
    if uri.startswith("file://"):
        from urllib.parse import unquote
        from urllib.request import url2pathname

        host_part = uri[7:]
        # Strip host name (usually empty or "localhost")
        if host_part.startswith("/"):
            return url2pathname(unquote(uri[7:]))
        # Windows: file:///C:/...
        return url2pathname(unquote(uri[7:]))

    return uri


def _render_flat_symbols(symbols: list[dict[str, Any]], file_path: str) -> str:
    """Render SymbolInformation[] (flat list with location)."""
    lines: list[str] = []
    for s in symbols:
        kind = s.get("kind", 0)
        label = _SYMBOL_KIND_LABELS.get(kind, f"kind-{kind}")
        name = s.get("name", "?")
        loc = s.get("location", {})
        rng = loc.get("range", {})
        start = rng.get("start", {})
        line = start.get("line", 0) + 1
        char = start.get("character", 0) + 1
        lines.append(f"  [{label}] {name} @ {line}:{char}")
    return "\n".join(lines)


def _render_hierarchical_symbols(
    symbols: list[dict[str, Any]],
    file_path: str,
    indent: int = 0,
) -> str:
    """Render DocumentSymbol[] with children nested."""
    lines: list[str] = []
    prefix = "  " * (indent + 1)

    for s in symbols:
        kind = s.get("kind", 0)
        label = _SYMBOL_KIND_LABELS.get(kind, f"kind-{kind}")
        name = s.get("name", "?")
        rng = s.get("range", {})
        start = rng.get("start", {})
        line = start.get("line", 0) + 1
        char = start.get("character", 0) + 1
        detail = s.get("detail", "")
        detail_str = f" — {detail}" if detail else ""
        lines.append(f"{prefix}[{label}] {name}{detail_str} @ {line}:{char}")

        children = s.get("children", [])
        if children:
            child_lines = _render_hierarchical_symbols(
                children, file_path, indent + 1
            )
            lines.append(child_lines)

    return "\n".join(lines)


def _count_symbols(symbols: list[dict[str, Any]]) -> int:
    """Count total symbols (including nested children)."""
    total = 0
    for s in symbols:
        total += 1
        children = s.get("children", [])
        if children:
            total += _count_symbols(children)
    return total
