"""FIFO notes store — lightweight cross-turn agent memory.

Two scopes:
- *workspace*: ``.rcoder/notes.json`` — project-specific
- *global*:   ``~/.rcoder/notes.json`` — cross-project preferences
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _notes_path(scope: str, workspace_dir: Path | None = None) -> Path:
    if scope == "global":
        return Path.home() / ".rcoder" / "notes.json"
    root = workspace_dir or Path.cwd()
    return root / ".rcoder" / "notes.json"


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: Path, entries: list[dict]) -> None:
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)


def write_note(
    content: str,
    *,
    scope: str = "workspace",
    max_entries: int = 30,
    workspace_dir: Path | None = None,
) -> None:
    """Append a note and trim the store to *max_entries* (FIFO)."""
    path = _notes_path(scope, workspace_dir)
    entries = _load(path)
    entries.append(
        {
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    if len(entries) > max_entries:
        entries = entries[-max_entries:]
    _save(path, entries)


def read_notes(
    scope: str = "workspace",
    workspace_dir: Path | None = None,
) -> list[dict]:
    """Return all notes for *scope* (most recent last)."""
    return _load(_notes_path(scope, workspace_dir))


def render_notes(
    workspace_dir: Path | None = None,
) -> str | None:
    """Render workspace + global notes as text for context injection.

    Returns *None* when both scopes are empty so the caller can skip the block.
    """
    ws_entries = read_notes("workspace", workspace_dir)
    gl_entries = read_notes("global", workspace_dir)

    if not ws_entries and not gl_entries:
        return None

    lines: list[str] = []

    if ws_entries:
        lines.append(f"Workspace notes ({len(ws_entries)}):")
        for i, e in enumerate(ws_entries, 1):
            ts = e.get("ts", "")[:16].replace("T", " ")
            lines.append(f'  [{i}] {ts}  "{e["content"]}"')

    if gl_entries:
        if ws_entries:
            lines.append("")
        lines.append(f"Global notes ({len(gl_entries)}):")
        for i, e in enumerate(gl_entries, 1):
            ts = e.get("ts", "")[:16].replace("T", " ")
            lines.append(f'  [{i}] {ts}  "{e["content"]}"')

    return "\n".join(lines)


def delete_note(
    index: int,
    *,
    scope: str = "workspace",
    workspace_dir: Path | None = None,
) -> bool:
    """Delete a single note by 1-based index.  Returns True on success."""
    path = _notes_path(scope, workspace_dir)
    entries = _load(path)
    if not entries or index < 1 or index > len(entries):
        return False
    entries.pop(index - 1)
    _save(path, entries)
    return True
