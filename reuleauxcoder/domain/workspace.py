"""Platform-neutral workspace primitive contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Protocol


class WorkspaceErrorCode(str, Enum):
    INVALID_PATH = "invalid_path"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_UNIQUE = "not_unique"
    IO_ERROR = "io_error"


class WorkspaceError(Exception):
    def __init__(self, code: WorkspaceErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    path: str
    relative_path: str
    name: str
    is_file: bool
    is_dir: bool
    size: int
    mtime: float
    mode: int


@dataclass(frozen=True, slots=True)
class WorkspaceListResult:
    entries: tuple[WorkspaceEntry, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceSearchMatch:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True, slots=True)
class WorkspaceSearchResult:
    matches: tuple[WorkspaceSearchMatch, ...]
    truncated: bool = False


class WorkspacePort(Protocol):
    root: Path

    def resolve(self, path: str | Path) -> Path: ...

    def read_text(self, path: str | Path) -> str: ...

    def stat_entry(self, path: str | Path) -> WorkspaceEntry: ...

    def write_text_atomic(self, path: str | Path, content: str) -> str: ...

    def replace_exact_atomic(
        self, path: str | Path, old: str, new: str
    ) -> tuple[str, str]: ...

    def list_entries(
        self,
        path: str | Path,
        *,
        recursive: bool = False,
        include_hidden: bool = True,
        max_entries: int = 10_000,
    ) -> WorkspaceListResult: ...

    def search_text(
        self,
        pattern: str,
        path: str | Path,
        *,
        include: str | None = None,
        exclude_dirs: tuple[str, ...] = (),
        max_files: int = 5_000,
        max_matches: int = 200,
    ) -> WorkspaceSearchResult: ...


def search_text_via_primitives(
    workspace: WorkspacePort,
    pattern: str,
    path: str | Path,
    *,
    include: str | None = None,
    exclude_dirs: tuple[str, ...] = (),
    max_files: int = 5_000,
    max_matches: int = 200,
) -> WorkspaceSearchResult:
    """Apply Host regex semantics using only stat/list/read primitives."""
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_PATH, f"invalid regex: {error}"
        ) from error
    if max_files < 1 or max_matches < 1:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_PATH,
            "max_files and max_matches must be positive",
        )

    base = workspace.stat_entry(path)
    listing_truncated = False
    if base.is_file:
        files = [base]
    elif base.is_dir:
        listing = workspace.list_entries(
            path,
            recursive=True,
            include_hidden=True,
            max_entries=max_files * 4,
        )
        listing_truncated = listing.truncated
        excluded = set(exclude_dirs)
        candidates = [
            entry
            for entry in listing.entries
            if entry.is_file
            and not any(
                part in excluded for part in Path(entry.relative_path).parts
            )
            and (include is None or Path(entry.relative_path).match(include))
        ]
        listing_truncated = listing_truncated or len(candidates) > max_files
        files = candidates[:max_files]
    else:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_A_FILE, f"{path} is not searchable"
        )

    matches: list[WorkspaceSearchMatch] = []
    for entry in files:
        try:
            text = workspace.read_text(entry.path)
        except WorkspaceError as error:
            if error.code in {WorkspaceErrorCode.NOT_FOUND, WorkspaceErrorCode.IO_ERROR}:
                continue
            raise
        for line_number, line in enumerate(text.splitlines(), 1):
            if not regex.search(line):
                continue
            matches.append(
                WorkspaceSearchMatch(
                    path=entry.path,
                    line_number=line_number,
                    line=line.rstrip(),
                )
            )
            if len(matches) >= max_matches:
                return WorkspaceSearchResult(tuple(matches), truncated=True)
    return WorkspaceSearchResult(tuple(matches), truncated=listing_truncated)
