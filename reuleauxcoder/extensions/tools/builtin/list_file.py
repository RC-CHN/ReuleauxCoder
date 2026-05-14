"""Pure-Python directory listing tool — no shell, always read-only."""

from __future__ import annotations

import datetime
import fnmatch
import os
import re
import stat
from pathlib import Path

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
    ) -> str:
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
    ) -> str:
        return self.backend.exec_tool(
            "list_file",
            {
                "path": path,
                "all": all,
                "long": long,
                "recursive": recursive,
                "pattern": pattern,
            },
        )

    @backend_handler("local")
    def _execute_local(
        self,
        path: str = ".",
        all: bool = True,
        long: bool = True,
        recursive: bool = False,
        pattern: str | None = None,
    ) -> str:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            return f"Error: '{path}' does not exist"

        # Single file
        if resolved.is_file():
            return self._format_single(resolved, long=long)

        # Directory
        lines = self._list_dir(resolved, all=all, long=long, pattern=pattern)
        if not lines:
            note = (
                f"(no entries matching '{pattern}' in '{path}')"
                if pattern
                else f"(empty directory: '{path}')"
            )
            return note

        header = f"{resolved}{'/' if resolved.is_dir() else ''}:\n" if long else ""
        output = header + "\n".join(lines)

        if recursive:
            subdirs = self._collect_subdirs(resolved, all=all)
            for sub in sorted(subdirs):
                sub_output = self._execute_local(
                    path=str(sub),
                    all=all,
                    long=long,
                    recursive=True,
                    pattern=pattern,
                )
                output += "\n\n" + sub_output

        return output

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _format_single(resolved: Path, *, long: bool) -> str:
        st = resolved.stat()
        name = _sanitize_name(resolved.name)
        if long:
            return (
                f"{_format_mode(st.st_mode)}  "
                f"{st.st_size:>8}  "
                f"{_format_mtime(st.st_mtime)}  "
                f"{name}"
            )
        return name

    @staticmethod
    def _list_dir(
        root: Path,
        *,
        all: bool,
        long: bool,
        pattern: str | None,
    ) -> list[str]:
        """Collect and format entries in *root*.  Returns formatted lines."""
        entries: list[tuple[str, int, int, float]] = []  # name, mode, size, mtime
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not all and entry.name.startswith("."):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    if pattern and not fnmatch.fnmatch(entry.name, pattern):
                        continue
                    entries.append((entry.name, st.st_mode, st.st_size, st.st_mtime))
        except PermissionError:
            return [f"(permission denied: {root})"]
        except OSError as exc:
            return [f"(error: {exc})"]

        if not entries:
            return []

        # Sort: directories first, then by name (case-insensitive)
        entries.sort(key=lambda e: (not stat.S_ISDIR(e[1]), e[0].lower()))

        if long:
            return [
                f"{_format_mode(mode)}  {size:>8}  {_format_mtime(mtime)}  {_sanitize_name(name)}"
                f"{'/' if stat.S_ISDIR(mode) else ''}"
                for name, mode, size, mtime in entries
            ]
        else:
            return [
                f"{_sanitize_name(name)}{'/' if stat.S_ISDIR(mode) else ''}"
                for name, mode, _size, _mtime in entries
            ]

    @staticmethod
    def _collect_subdirs(root: Path, *, all: bool) -> list[Path]:
        """Return subdirectories to recurse into."""
        subdirs: list[Path] = []
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not all and entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(root / entry.name)
        except OSError:
            pass
        return subdirs
