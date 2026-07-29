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
    REVISION_CONFLICT = "revision_conflict"
    IO_ERROR = "io_error"


class WorkspaceError(Exception):
    def __init__(
        self,
        code: WorkspaceErrorCode,
        message: str,
        *,
        mutation_receipt: "WorkspaceMutationReceipt | None" = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.mutation_receipt = mutation_receipt


class WorkspaceMutationVerification(str, Enum):
    APPLIED_VERIFIED = "applied_verified"
    FAILED_UNCHANGED = "failed_unchanged"
    DIVERGED = "diverged"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WorkspaceRevision:
    """Content identity observed by the backend for one workspace target."""

    exists: bool
    sha256: str | None
    size_bytes: int
    mtime_ns: int | None = None

    def same_content(self, other: "WorkspaceRevision") -> bool:
        return (
            self.exists == other.exists
            and self.sha256 == other.sha256
            and self.size_bytes == other.size_bytes
        )

    @property
    def short_sha256(self) -> str:
        return self.sha256[:12] if self.sha256 else "missing"

    def to_dict(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkspaceRevision":
        sha256 = data.get("sha256")
        mtime_ns = data.get("mtime_ns")
        return cls(
            exists=bool(data.get("exists")),
            sha256=sha256 if isinstance(sha256, str) else None,
            size_bytes=int(data.get("size_bytes") or 0),
            mtime_ns=int(mtime_ns) if isinstance(mtime_ns, int) else None,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceDocumentSnapshot:
    """Text and revision derived from the same observed file bytes."""

    resolved_path: str
    content: str | None
    revision: WorkspaceRevision


@dataclass(frozen=True, slots=True)
class WorkspaceMutationReceipt:
    """Backend-observed facts for one attempted file mutation."""

    resolved_path: str
    before: WorkspaceRevision
    intended_after_sha256: str
    intended_size_bytes: int
    observed_after: WorkspaceRevision | None
    atomic_replace: bool
    verification: WorkspaceMutationVerification
    expected_before: WorkspaceRevision | None = None
    external_change_before_write: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "resolved_path": self.resolved_path,
            "before": self.before.to_dict(),
            "intended_after_sha256": self.intended_after_sha256,
            "intended_size_bytes": self.intended_size_bytes,
            "observed_after": (
                self.observed_after.to_dict()
                if self.observed_after is not None
                else None
            ),
            "atomic_replace": self.atomic_replace,
            "verification": self.verification.value,
            "expected_before": (
                self.expected_before.to_dict()
                if self.expected_before is not None
                else None
            ),
            "external_change_before_write": self.external_change_before_write,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "WorkspaceMutationReceipt":
        before = data.get("before")
        observed_after = data.get("observed_after")
        expected_before = data.get("expected_before")
        if not isinstance(before, dict):
            raise ValueError("mutation receipt is missing before revision")
        return cls(
            resolved_path=str(data.get("resolved_path") or ""),
            before=WorkspaceRevision.from_dict(before),
            intended_after_sha256=str(data.get("intended_after_sha256") or ""),
            intended_size_bytes=int(data.get("intended_size_bytes") or 0),
            observed_after=(
                WorkspaceRevision.from_dict(observed_after)
                if isinstance(observed_after, dict)
                else None
            ),
            atomic_replace=bool(data.get("atomic_replace")),
            verification=WorkspaceMutationVerification(
                str(data.get("verification") or "unknown")
            ),
            expected_before=(
                WorkspaceRevision.from_dict(expected_before)
                if isinstance(expected_before, dict)
                else None
            ),
            external_change_before_write=bool(
                data.get("external_change_before_write")
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceMutationResult:
    """Textual before/after state plus a verified mutation receipt."""

    old_content: str | None
    new_content: str
    receipt: WorkspaceMutationReceipt


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

    def snapshot_text(self, path: str | Path) -> WorkspaceDocumentSnapshot: ...

    def stat_entry(self, path: str | Path) -> WorkspaceEntry: ...

    def write_text_atomic(self, path: str | Path, content: str) -> str: ...

    def write_text_verified(
        self,
        path: str | Path,
        content: str,
        *,
        expected_revision: WorkspaceRevision | None = None,
    ) -> WorkspaceMutationResult: ...

    def replace_exact_atomic(
        self, path: str | Path, old: str, new: str
    ) -> tuple[str, str]: ...

    def replace_exact_verified(
        self,
        path: str | Path,
        old: str,
        new: str,
        *,
        expected_revision: WorkspaceRevision | None = None,
    ) -> WorkspaceMutationResult: ...

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
