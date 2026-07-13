"""Platform-neutral workspace primitive contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fnmatch import translate as fnmatch_translate
from functools import lru_cache
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
class WorkspaceGlobResult:
    entries: tuple[WorkspaceEntry, ...]
    match_count: int
    listing_truncated: bool = False

    @property
    def truncated(self) -> bool:
        return self.listing_truncated or self.match_count > len(self.entries)


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

    def glob_paths(
        self,
        pattern: str,
        path: str | Path,
        *,
        max_entries: int = 20_000,
        max_matches: int = 100,
    ) -> WorkspaceGlobResult: ...

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


class PortableGlobMatcher:
    """Precompiled segment matcher with stable ``**`` semantics."""

    def __init__(self, pattern: str) -> None:
        pattern_parts = tuple(
            part for part in pattern.replace("\\", "/").split("/") if part
        )
        self._segments: tuple[re.Pattern[str] | None, ...] = tuple(
            None if part == "**" else re.compile(fnmatch_translate(part))
            for part in pattern_parts
        )

    def matches(self, relative_path: str) -> bool:
        path_parts = tuple(
            part for part in relative_path.replace("\\", "/").split("/") if part
        )
        if not path_parts or not self._segments:
            return False
        previous = [False] * (len(self._segments) + 1)
        previous[0] = True
        for pattern_index, segment in enumerate(self._segments, 1):
            if segment is None:
                previous[pattern_index] = previous[pattern_index - 1]
        for part in path_parts:
            current = [False] * (len(self._segments) + 1)
            for pattern_index, segment in enumerate(self._segments, 1):
                if segment is None:
                    current[pattern_index] = (
                        current[pattern_index - 1] or previous[pattern_index]
                    )
                else:
                    current[pattern_index] = (
                        previous[pattern_index - 1]
                        and segment.fullmatch(part) is not None
                    )
            previous = current
        return previous[-1]


@lru_cache(maxsize=256)
def compile_portable_glob(pattern: str) -> PortableGlobMatcher:
    return PortableGlobMatcher(pattern)


def portable_glob_match(relative_path: str, pattern: str) -> bool:
    """Match path segments with stable ``**`` semantics on every platform."""
    return compile_portable_glob(pattern).matches(relative_path)


def glob_paths_via_primitives(
    workspace: WorkspacePort,
    pattern: str,
    path: str | Path,
    *,
    max_entries: int = 20_000,
    max_matches: int = 100,
) -> WorkspaceGlobResult:
    """Portable compatibility projection built from stat/list primitives."""
    base = workspace.stat_entry(path)
    if not base.is_dir:
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_A_DIRECTORY, f"{path} is not a directory"
        )
    listing = workspace.list_entries(
        path,
        recursive=True,
        include_hidden=True,
        max_entries=max_entries,
    )
    hits = [
        entry
        for entry in listing.entries
        if portable_glob_match(entry.relative_path, pattern)
    ]
    hits.sort(key=lambda entry: entry.mtime, reverse=True)
    return WorkspaceGlobResult(
        entries=tuple(hits[:max_matches]),
        match_count=len(hits),
        listing_truncated=listing.truncated,
    )


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
            and not any(part in excluded for part in Path(entry.relative_path).parts)
            and (include is None or Path(entry.relative_path).match(include))
        ]
        listing_truncated = listing_truncated or len(candidates) > max_files
        files = candidates[:max_files]
    else:
        raise WorkspaceError(WorkspaceErrorCode.NOT_A_FILE, f"{path} is not searchable")

    matches: list[WorkspaceSearchMatch] = []
    for entry in files:
        try:
            text = workspace.read_text(entry.path)
        except WorkspaceError as error:
            if error.code in {
                WorkspaceErrorCode.NOT_FOUND,
                WorkspaceErrorCode.IO_ERROR,
            }:
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
