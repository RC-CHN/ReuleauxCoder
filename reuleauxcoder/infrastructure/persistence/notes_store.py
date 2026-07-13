"""Durable two-scope notes used by the ephemeral execution overlay.

The stores are deliberately independent:

* ``workspace`` belongs to the project root that launched the agent.
* ``global`` belongs to the user home and is shared across projects.

Both files use the same versioned JSON schema, but have separate limits,
locks, migrations, and identifiers.  A shell ``cd`` must never change which
workspace store an agent is using.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any


_STORE_VERSION = 2
_VALID_SCOPES = frozenset({"workspace", "global"})
_LOCK_TIMEOUT_SECONDS = 3.0
_STALE_LOCK_SECONDS = 30.0
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class NotesStoreError(RuntimeError):
    """Raised when durable notes cannot be read or safely mutated."""


@dataclass(frozen=True)
class NoteEntry:
    """One stable note record."""

    id: str
    content: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_scope(scope: str) -> str:
    normalized = str(scope).strip().lower()
    if normalized not in _VALID_SCOPES:
        raise ValueError("scope must be 'workspace' or 'global'")
    return normalized


def _notes_path(scope: str, workspace_dir: Path | None = None) -> Path:
    """Compatibility path helper for callers that use the module functions."""
    normalized = _validate_scope(scope)
    if normalized == "global":
        return Path.home() / ".rcoder" / "notes.json"
    root = Path(workspace_dir) if workspace_dir is not None else Path.cwd()
    return root / ".rcoder" / "notes.json"


def _process_lock(path: Path) -> threading.RLock:
    key = path.resolve(strict=False)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize cross-process mutations with a small portable lock file."""
    lock_path = path.with_name(f"{path.name}.lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"{os.getpid()} {time.time()}\n".encode("ascii"))
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > _STALE_LOCK_SECONDS
                if stale:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise NotesStoreError(f"timed out waiting for notes lock: {path}")
            time.sleep(0.02)
        except OSError as exc:
            raise NotesStoreError(f"cannot lock notes store {path}: {exc}") from exc
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _legacy_id(scope: str, index: int, content: str, timestamp: str) -> str:
    payload = f"{scope}\0{index}\0{timestamp}\0{content}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    prefix = "wn" if scope == "workspace" else "gn"
    return f"{prefix}_{digest}"


def _new_id(scope: str) -> str:
    prefix = "wn" if scope == "workspace" else "gn"
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _decode_entries(data: Any, *, scope: str, path: Path) -> list[NoteEntry]:
    legacy = isinstance(data, list)
    if legacy:
        raw_entries = data
    elif isinstance(data, dict):
        version = data.get("version")
        file_scope = data.get("scope")
        if version != _STORE_VERSION:
            raise NotesStoreError(
                f"unsupported notes schema version {version!r} in {path}"
            )
        if file_scope != scope:
            raise NotesStoreError(
                f"notes scope mismatch in {path}: expected {scope}, got {file_scope!r}"
            )
        raw_entries = data.get("notes")
    else:
        raise NotesStoreError(f"notes store must contain a JSON object or list: {path}")

    if not isinstance(raw_entries, list):
        raise NotesStoreError(f"notes field must be a list: {path}")

    entries: list[NoteEntry] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_entries):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise NotesStoreError(f"invalid note at index {index + 1} in {path}")
        content = item["content"]
        legacy_ts = str(item.get("ts") or "")
        created_at = str(
            item.get("created_at") or legacy_ts or "1970-01-01T00:00:00+00:00"
        )
        updated_at = str(item.get("updated_at") or created_at)
        note_id = str(item.get("id") or "")
        if legacy or not note_id:
            note_id = _legacy_id(scope, index, content, created_at)
        if note_id in seen_ids:
            raise NotesStoreError(f"duplicate note id {note_id!r} in {path}")
        expected_prefix = "wn_" if scope == "workspace" else "gn_"
        if not note_id.startswith(expected_prefix):
            raise NotesStoreError(
                f"note id {note_id!r} does not belong to {scope} scope in {path}"
            )
        seen_ids.add(note_id)
        entries.append(
            NoteEntry(
                id=note_id,
                content=content,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
    return entries


def _load(path: Path, *, scope: str = "workspace") -> list[NoteEntry]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise NotesStoreError(f"invalid JSON in notes store {path}: {exc}") from exc
    except OSError as exc:
        raise NotesStoreError(f"cannot read notes store {path}: {exc}") from exc
    return _decode_entries(data, scope=scope, path=path)


def _save(path: Path, entries: list[NoteEntry], *, scope: str = "workspace") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _STORE_VERSION,
        "scope": scope,
        "notes": [entry.to_dict() for entry in entries],
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise NotesStoreError(f"cannot save notes store {path}: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


class NoteStore:
    """Bound, durable workspace + global notes repository."""

    def __init__(
        self,
        workspace_dir: Path,
        *,
        home_dir: Path | None = None,
        workspace_max: int = 30,
        global_max: int = 20,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).expanduser().resolve(strict=False)
        self.home_dir = (home_dir or Path.home()).expanduser().resolve(strict=False)
        self.workspace_max = max(1, int(workspace_max))
        self.global_max = max(1, int(global_max))

    def path_for(self, scope: str) -> Path:
        normalized = _validate_scope(scope)
        if normalized == "global":
            return self.home_dir / ".rcoder" / "notes.json"
        return self.workspace_dir / ".rcoder" / "notes.json"

    def max_for(self, scope: str) -> int:
        return (
            self.global_max
            if _validate_scope(scope) == "global"
            else self.workspace_max
        )

    def read(self, scope: str = "workspace") -> list[NoteEntry]:
        normalized = _validate_scope(scope)
        return _load(self.path_for(normalized), scope=normalized)

    def write(self, content: str, *, scope: str = "workspace") -> NoteEntry:
        normalized = _validate_scope(scope)
        normalized_content = str(content).strip()
        if not normalized_content:
            raise ValueError("note content must not be empty")
        path = self.path_for(normalized)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _process_lock(path), _exclusive_file_lock(path):
            entries = _load(path, scope=normalized)
            now = _utc_now()
            entry = NoteEntry(
                id=_new_id(normalized),
                content=normalized_content,
                created_at=now,
                updated_at=now,
            )
            entries.append(entry)
            entries = entries[-self.max_for(normalized) :]
            _save(path, entries, scope=normalized)
        return entry

    def edit(
        self, note_id: str, content: str, *, scope: str = "workspace"
    ) -> NoteEntry | None:
        normalized = _validate_scope(scope)
        normalized_id = str(note_id).strip()
        normalized_content = str(content).strip()
        if not normalized_id:
            raise ValueError("note_id must not be empty")
        if not normalized_content:
            raise ValueError("note content must not be empty")
        path = self.path_for(normalized)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _process_lock(path), _exclusive_file_lock(path):
            entries = _load(path, scope=normalized)
            for index, entry in enumerate(entries):
                if entry.id != normalized_id:
                    continue
                updated = NoteEntry(
                    id=entry.id,
                    content=normalized_content,
                    created_at=entry.created_at,
                    updated_at=_utc_now(),
                )
                entries[index] = updated
                _save(path, entries, scope=normalized)
                return updated
        return None

    def delete(
        self,
        *,
        scope: str = "workspace",
        note_id: str | None = None,
        index: int | None = None,
    ) -> NoteEntry | None:
        normalized = _validate_scope(scope)
        if not note_id and index is None:
            raise ValueError("note_id or index is required")
        path = self.path_for(normalized)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _process_lock(path), _exclusive_file_lock(path):
            entries = _load(path, scope=normalized)
            target_index: int | None = None
            if note_id:
                target_index = next(
                    (i for i, entry in enumerate(entries) if entry.id == note_id), None
                )
            elif index is not None and 1 <= index <= len(entries):
                target_index = index - 1
            if target_index is None:
                return None
            removed = entries.pop(target_index)
            _save(path, entries, scope=normalized)
            return removed

    @staticmethod
    def _render_scope(
        scope: str, entries: list[NoteEntry], *, max_chars: int
    ) -> str:
        title = "Workspace notes" if scope == "workspace" else "Global notes"
        lines = [f"{title} ({len(entries)}, newest first):"]
        remaining = max(0, max_chars - len(lines[0]))
        rendered = 0
        for entry in reversed(entries):
            compact = " ↵ ".join(entry.content.splitlines())
            prefix = f"  [{entry.id}] "
            available = remaining - len(prefix) - 1
            if available <= 1:
                break
            if len(compact) > available:
                if rendered:
                    break
                compact = compact[: max(1, available - 1)] + "…"
            line = prefix + compact
            lines.append(line)
            remaining -= len(line) + 1
            rendered += 1
        omitted = len(entries) - rendered
        marker = f"  … {omitted} older note(s) omitted"
        if omitted and remaining >= len(marker) + 1:
            lines.append(marker)
        return "\n".join(lines)

    def render(self, *, max_chars: int = 1_200) -> str | None:
        """Render both scopes with independent budgets and stable IDs."""
        budget = max(120, int(max_chars))
        workspace = self.read("workspace")
        global_entries = self.read("global")
        if not workspace and not global_entries:
            return None
        if workspace and global_entries:
            separator = "\n\n"
            first_budget = max(60, (budget - len(separator)) // 2)
            second_budget = max(60, budget - len(separator) - first_budget)
            first = self._render_scope(
                "workspace", workspace, max_chars=first_budget
            )
            second = self._render_scope(
                "global", global_entries, max_chars=second_budget
            )
            return (first + separator + second)[:budget]
        scope = "workspace" if workspace else "global"
        entries = workspace if workspace else global_entries
        return self._render_scope(scope, entries, max_chars=budget)[:budget]


def _compat_store(
    workspace_dir: Path | None = None,
    *,
    workspace_max: int = 30,
    global_max: int = 20,
) -> NoteStore:
    return NoteStore(
        workspace_dir or Path.cwd(),
        workspace_max=workspace_max,
        global_max=global_max,
    )


def write_note(
    content: str,
    *,
    scope: str = "workspace",
    max_entries: int = 30,
    workspace_dir: Path | None = None,
) -> NoteEntry:
    """Compatibility wrapper that writes one note to the requested scope."""
    kwargs = (
        {"global_max": max_entries}
        if _validate_scope(scope) == "global"
        else {"workspace_max": max_entries}
    )
    return _compat_store(workspace_dir, **kwargs).write(content, scope=scope)


def read_notes(
    scope: str = "workspace",
    workspace_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Compatibility wrapper returning JSON-ready note dictionaries."""
    return [entry.to_dict() for entry in _compat_store(workspace_dir).read(scope)]


def render_notes(
    workspace_dir: Path | None = None,
    *,
    max_chars: int = 1_200,
) -> str | None:
    """Compatibility wrapper rendering workspace and global notes."""
    return _compat_store(workspace_dir).render(max_chars=max_chars)


def edit_note(
    note_id: str,
    content: str,
    *,
    scope: str = "workspace",
    workspace_dir: Path | None = None,
) -> bool:
    """Compatibility wrapper editing a note by stable identifier."""
    return _compat_store(workspace_dir).edit(note_id, content, scope=scope) is not None


def delete_note(
    index: int | None = None,
    *,
    note_id: str | None = None,
    scope: str = "workspace",
    workspace_dir: Path | None = None,
) -> bool:
    """Compatibility wrapper deleting by stable ID or legacy 1-based index."""
    return (
        _compat_store(workspace_dir).delete(
            scope=scope, note_id=note_id, index=index
        )
        is not None
    )
