"""Pure-Python directory listing tool — no shell, always read-only."""

from __future__ import annotations

import datetime
import fnmatch
import re
import stat
from pathlib import Path, PurePath

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.workspace import WorkspaceEntry, WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


_SANITIZE_RE = re.compile(r"[`*_\[\]|<>]")


def _sanitize_name(name: str) -> str:
    """Escape characters that could interfere with markdown rendering."""
    return _SANITIZE_RE.sub(r"\\\g<0>", name)


def _format_mode(mode: int) -> str:
    """Convert a ``stat`` mode to an ``ls -l``-style permission string."""
    kind = "d" if stat.S_ISDIR(mode) else "-"
    perms = (
        ("r" if mode & stat.S_IRUSR else "-")
        + ("w" if mode & stat.S_IWUSR else "-")
        + ("x" if mode & stat.S_IXUSR else "-")
        + ("r" if mode & stat.S_IRGRP else "-")
        + ("w" if mode & stat.S_IWGRP else "-")
        + ("x" if mode & stat.S_IXGRP else "-")
        + ("r" if mode & stat.S_IROTH else "-")
        + ("w" if mode & stat.S_IWOTH else "-")
        + ("x" if mode & stat.S_IXOTH else "-")
    )
    return kind + perms


def _format_mtime(ts: float) -> str:
    """Short human-readable modification time."""
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime("%b %d %H:%M")


@register_tool
class ListFileTool(Tool):
    name = "list_file"
    description = (
        "List files and directories. Pure read-only — no shell involved, always safe. "
        "Use this for exploring project structure, checking what files exist, "
        "or verifying paths.  Prefer this over `shell ls` — it is faster, safer, "
        "and returns structured output that is easier for the model to consume."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Directory to list, or a single file to show its info. "
                    "Defaults to the current working directory."
                ),
            },
            "all": {
                "type": "boolean",
                "description": "Show hidden files starting with '.' (default: true)",
            },
            "long": {
                "type": "boolean",
                "description": (
                    "Show permissions, size, and modification time (default: true). "
                    "When false, only names are printed."
                ),
            },
            "recursive": {
                "type": "boolean",
                "description": (
                    "Recursively list subdirectories (default: false). "
                    "Entries are printed with their path relative to *path*."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Shell-style glob pattern to filter entries, "
                    'e.g. "*.py" or "**/test_*".  Only the filename is '
                    "matched by default; when *recursive* is true the "
                    "full relative path is matched."
                ),
            },
        },
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self,
        path: str = ".",
        all: bool = True,
        long: bool = True,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> str | ToolOutcome:
        return self.run_backend(
            path=path,
            all=all,
            long=long,
            recursive=recursive,
            pattern=pattern,
        )

    @backend_handler("remote_relay")
    def _execute_remote(
        self,
        path: str = ".",
        all: bool = True,
        long: bool = True,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> str | ToolOutcome:
        return self._execute_workspace(path, all, long, recursive, pattern)

    @backend_handler("local")
    def _execute_local(
        self,
        path: str = ".",
        all: bool = True,
        long: bool = True,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> str | ToolOutcome:
        return self._execute_workspace(path, all, long, recursive, pattern)

    def _execute_workspace(
        self,
        path: str,
        all: bool,
        long: bool,
        recursive: bool,
        pattern: str | None,
    ) -> str | ToolOutcome:
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        if pattern is not None and not isinstance(pattern, str):
            return "Error: pattern must be a string when provided"
        try:
            base = self.backend.workspace.stat_entry(path)
            if base.is_file:
                return ToolOutcome(
                    summary=f"Listed 1 entry at {path}",
                    content=self._format_single(base, long=long),
                    metadata={"operation": "list", "path": path, "entry_count": 1},
                    retention_hint=ToolRetentionHint(
                        strategy=ToolRetentionStrategy.HEAD_TAIL
                    ),
                )
            if not base.is_dir:
                return f"Error: '{path}' is not a directory"
            listing = self.backend.workspace.list_entries(
                path,
                recursive=recursive,
                include_hidden=all,
                max_entries=20_000,
            )
            entries = [
                entry
                for entry in listing.entries
                if self._matches(entry, pattern, recursive=recursive)
            ]
            if not entries:
                content = (
                    f"(no entries matching '{pattern}' in '{path}')"
                    if pattern
                    else f"(empty directory: '{path}')"
                )
                return ToolOutcome(
                    summary=f"Listed 0 entries in {path}",
                    content=content,
                    metadata={"operation": "list", "path": path, "entry_count": 0},
                    retention_hint=ToolRetentionHint(
                        strategy=ToolRetentionStrategy.HEAD_TAIL
                    ),
                )
            entries.sort(
                key=lambda entry: (
                    str(PurePath(entry.relative_path).parent).lower(),
                    not entry.is_dir,
                    entry.name.lower(),
                )
            )
            if recursive and not long:
                output = "\n".join(
                    self._format_entry(
                        entry,
                        long=False,
                        display_name=entry.relative_path,
                    )
                    for entry in entries
                )
            elif recursive:
                output = self._format_recursive(base, entries)
            else:
                header = f"{base.path}/:\n" if long else ""
                output = header + "\n".join(
                    self._format_entry(entry, long=long) for entry in entries
                )
            if listing.truncated:
                output += "\n... (workspace listing limit reached)"
            return ToolOutcome(
                summary=(
                    f"Listed {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
                    f"in {path}"
                ),
                content=output,
                metadata={
                    "operation": "list",
                    "path": path,
                    "entry_count": len(entries),
                    "recursive": recursive,
                    "pattern": pattern,
                    "truncated": listing.truncated,
                },
                retention_hint=ToolRetentionHint(
                    strategy=ToolRetentionStrategy.HEAD_TAIL
                ),
            )
        except WorkspaceError as error:
            if error.code.value == "not_found":
                return f"Error: '{path}' does not exist"
            return f"Error [{error.code.value}]: {error.message}"
        except Exception as error:
            return f"Error: {error}"

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _format_single(entry: WorkspaceEntry, *, long: bool) -> str:
        return ListFileTool._format_entry(entry, long=long)

    @staticmethod
    def _format_entry(
        entry: WorkspaceEntry,
        *,
        long: bool,
        display_name: str | None = None,
    ) -> str:
        name = _sanitize_name(display_name or entry.name)
        suffix = "/" if entry.is_dir else ""
        if not long:
            return name + suffix
        return (
            f"{_format_mode(entry.mode)}  {entry.size:>8}  "
            f"{_format_mtime(entry.mtime)}  {name}{suffix}"
        )

    @staticmethod
    def _matches(
        entry: WorkspaceEntry, pattern: str | None, *, recursive: bool
    ) -> bool:
        if pattern is None:
            return True
        candidate = entry.relative_path if recursive else entry.name
        return fnmatch.fnmatch(candidate, pattern)

    @classmethod
    def _format_recursive(
        cls, base: WorkspaceEntry, entries: list[WorkspaceEntry]
    ) -> str:
        groups: dict[str, list[WorkspaceEntry]] = {}
        for entry in entries:
            parent = str(PurePath(entry.relative_path).parent)
            groups.setdefault(parent, []).append(entry)
        sections = []
        for parent, children in groups.items():
            directory = Path(base.path) if parent == "." else Path(base.path) / parent
            lines = "\n".join(cls._format_entry(item, long=True) for item in children)
            sections.append(f"{directory}/:\n{lines}")
        return "\n\n".join(sections)
