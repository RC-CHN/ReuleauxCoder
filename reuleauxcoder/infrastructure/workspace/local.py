"""Filesystem-backed, root-confined WorkspacePort."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from fnmatch import translate as fnmatch_translate

from reuleauxcoder.domain.workspace import (
    WorkspaceEntry,
    WorkspaceDocumentSnapshot,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceGlobResult,
    WorkspaceListResult,
    WorkspaceMutationReceipt,
    WorkspaceMutationResult,
    WorkspaceMutationVerification,
    WorkspaceRevision,
    WorkspaceSearchMatch,
    WorkspaceSearchResult,
    compile_portable_glob,
)


class LocalWorkspacePort:
    def __init__(self, root: str | Path, *, cwd: str | Path | None = None):
        self.root = Path(root).expanduser().resolve()
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else self.root
        self._external_path_grants: ContextVar[frozenset[Path]] = ContextVar(
            f"workspace_external_path_grants_{id(self)}", default=frozenset()
        )
        try:
            self.cwd.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"cwd escapes workspace root: {self.cwd}",
            ) from error

    def resolve(self, path: str | Path) -> Path:
        resolved = self._resolve_candidate(path)
        if (
            self._is_inside_root(resolved)
            or resolved in self._external_path_grants.get()
        ):
            return resolved
        raise WorkspaceError(
            WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE,
            f"path escapes workspace root: {path}",
        )

    def external_path(self, path: str | Path) -> Path | None:
        """Return the normalized path only when it is outside the workspace."""
        resolved = self._resolve_candidate(path)
        return None if self._is_inside_root(resolved) else resolved

    @contextmanager
    def grant_external_path(self, path: str | Path) -> Iterator[Path]:
        """Temporarily grant this execution context one exact external path."""
        resolved = self._resolve_candidate(path)
        if self._is_inside_root(resolved):
            yield resolved
            return
        grants = self._external_path_grants.get()
        token = self._external_path_grants.set(grants | {resolved})
        try:
            yield resolved
        finally:
            self._external_path_grants.reset(token)

    def _resolve_candidate(self, path: str | Path) -> Path:
        if not isinstance(path, (str, Path)) or not str(path):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "path must be a non-empty string"
            )
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        try:
            return candidate.resolve()
        except (OSError, RuntimeError) as error:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH,
                f"failed to resolve path {path}: {error}",
            ) from error

    def _is_inside_root(self, resolved: Path) -> bool:
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return False
        return True

    def read_text(self, path: str | Path) -> str:
        resolved = self.resolve(path)
        if not resolved.exists():
            raise WorkspaceError(WorkspaceErrorCode.NOT_FOUND, f"{path} not found")
        if not resolved.is_file():
            raise WorkspaceError(WorkspaceErrorCode.NOT_A_FILE, f"{path} is not a file")
        try:
            with resolved.open(
                "r", encoding="utf-8", errors="replace", newline=""
            ) as stream:
                return stream.read()
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, f"failed to read {path}: {error}"
            ) from error

    def snapshot_text(self, path: str | Path) -> WorkspaceDocumentSnapshot:
        """Read text and a raw-byte revision from one backend observation."""
        resolved = self.resolve(path)
        try:
            data = resolved.read_bytes()
            metadata = resolved.stat()
        except FileNotFoundError:
            return WorkspaceDocumentSnapshot(
                resolved_path=str(resolved),
                content=None,
                revision=WorkspaceRevision(
                    exists=False,
                    sha256=None,
                    size_bytes=0,
                    mtime_ns=None,
                ),
            )
        except IsADirectoryError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_A_FILE, f"{path} is not a file"
            ) from error
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, f"failed to read {path}: {error}"
            ) from error
        return WorkspaceDocumentSnapshot(
            resolved_path=str(resolved),
            content=data.decode("utf-8", errors="replace"),
            revision=WorkspaceRevision(
                exists=True,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                mtime_ns=metadata.st_mtime_ns,
            ),
        )

    def stat_entry(self, path: str | Path) -> WorkspaceEntry:
        resolved = self.resolve(path)
        if not resolved.exists():
            raise WorkspaceError(WorkspaceErrorCode.NOT_FOUND, f"{path} not found")
        try:
            return self._entry(resolved, resolved.parent)
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, f"failed to stat {path}: {error}"
            ) from error

    def write_text_verified(
        self,
        path: str | Path,
        content: str,
        *,
        expected_revision: WorkspaceRevision | None = None,
    ) -> WorkspaceMutationResult:
        return self._write_text_verified(
            path,
            content,
            expected_revision=expected_revision,
            required_base_revision=None,
        )

    def _write_text_verified(
        self,
        path: str | Path,
        content: str,
        *,
        expected_revision: WorkspaceRevision | None,
        required_base_revision: WorkspaceRevision | None,
    ) -> WorkspaceMutationResult:
        if not isinstance(content, str):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "content must be a string"
            )
        resolved = self.resolve(path)
        before = self.snapshot_text(resolved)
        if required_base_revision is not None and not before.revision.same_content(
            required_base_revision
        ):
            raise WorkspaceError(
                WorkspaceErrorCode.REVISION_CONFLICT,
                f"{path} changed before the prepared edit could be committed",
            )
        encoded = content.encode("utf-8")
        intended_sha256 = hashlib.sha256(encoded).hexdigest()
        temporary: str | None = None
        atomic_replace = False
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{resolved.name}.", dir=resolved.parent
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                if resolved.exists():
                    os.chmod(temporary, resolved.stat().st_mode)
                os.replace(temporary, resolved)
                atomic_replace = True
                temporary = None
            except BaseException:
                raise
        except WorkspaceError:
            raise
        except OSError as error:
            self._unlink_temporary(temporary)
            receipt = self._mutation_receipt(
                resolved=resolved,
                before=before.revision,
                intended_sha256=intended_sha256,
                intended_size=len(encoded),
                expected_revision=expected_revision,
                atomic_replace=atomic_replace,
                write_failed=True,
            )
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR,
                f"failed to write {path}: {error}",
                mutation_receipt=receipt,
            ) from error
        finally:
            self._unlink_temporary(temporary)

        receipt = self._mutation_receipt(
            resolved=resolved,
            before=before.revision,
            intended_sha256=intended_sha256,
            intended_size=len(encoded),
            expected_revision=expected_revision,
            atomic_replace=atomic_replace,
            write_failed=False,
        )
        return WorkspaceMutationResult(
            old_content=before.content,
            new_content=content,
            receipt=receipt,
        )

    def replace_exact_verified(
        self,
        path: str | Path,
        old: str,
        new: str,
        *,
        expected_revision: WorkspaceRevision | None = None,
    ) -> WorkspaceMutationResult:
        if not isinstance(old, str) or not isinstance(new, str):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH,
                "old and new values must be strings",
            )
        if old == new:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "old and new values must differ"
            )
        for _attempt in range(3):
            snapshot = self.snapshot_text(path)
            if snapshot.content is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND, f"{path} not found"
                )
            occurrences = snapshot.content.count(old)
            if occurrences == 0:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND, "old text was not found"
                )
            if occurrences > 1:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_UNIQUE,
                    f"old text occurs {occurrences} times",
                )
            updated = snapshot.content.replace(old, new, 1)
            try:
                return self._write_text_verified(
                    path,
                    updated,
                    expected_revision=expected_revision,
                    required_base_revision=snapshot.revision,
                )
            except WorkspaceError as error:
                if error.code is not WorkspaceErrorCode.REVISION_CONFLICT:
                    raise
        raise WorkspaceError(
            WorkspaceErrorCode.REVISION_CONFLICT,
            f"{path} kept changing while the edit was being prepared",
        )

    @staticmethod
    def _unlink_temporary(temporary: str | None) -> None:
        if temporary is None:
            return
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            # The primary write result is more useful than a cleanup failure.
            pass

    def _mutation_receipt(
        self,
        *,
        resolved: Path,
        before: WorkspaceRevision,
        intended_sha256: str,
        intended_size: int,
        expected_revision: WorkspaceRevision | None,
        atomic_replace: bool,
        write_failed: bool,
    ) -> WorkspaceMutationReceipt:
        try:
            observed = self.snapshot_text(resolved).revision
        except (WorkspaceError, OSError):
            observed = None

        if observed is None:
            verification = WorkspaceMutationVerification.UNKNOWN
        else:
            intended = WorkspaceRevision(
                exists=True,
                sha256=intended_sha256,
                size_bytes=intended_size,
            )
            if observed.same_content(intended):
                verification = WorkspaceMutationVerification.APPLIED_VERIFIED
            elif write_failed and observed.same_content(before):
                verification = WorkspaceMutationVerification.FAILED_UNCHANGED
            else:
                verification = WorkspaceMutationVerification.DIVERGED

        return WorkspaceMutationReceipt(
            resolved_path=str(resolved),
            before=before,
            intended_after_sha256=intended_sha256,
            intended_size_bytes=intended_size,
            observed_after=observed,
            atomic_replace=atomic_replace,
            verification=verification,
            expected_before=expected_revision,
            external_change_before_write=(
                expected_revision is not None
                and not expected_revision.same_content(before)
            ),
        )

    def list_entries(
        self,
        path: str | Path,
        *,
        recursive: bool = False,
        include_hidden: bool = True,
        max_entries: int = 10_000,
    ) -> WorkspaceListResult:
        if max_entries < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "max_entries must be positive"
            )
        base = self.resolve(path)
        if not base.exists():
            raise WorkspaceError(WorkspaceErrorCode.NOT_FOUND, f"{path} not found")
        if base.is_file():
            return WorkspaceListResult((self._entry(base, base.parent),))
        if not base.is_dir():
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_A_DIRECTORY, f"{path} is not a directory"
            )

        entries: list[WorkspaceEntry] = []
        pending = [base]
        truncated = False
        try:
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name.lower())
                for child in children:
                    if not include_hidden and child.name.startswith("."):
                        continue
                    child_path = Path(child.path)
                    entries.append(self._entry(child_path, base))
                    if len(entries) >= max_entries:
                        truncated = True
                        pending.clear()
                        break
                    if recursive and child.is_dir(follow_symlinks=False):
                        pending.append(child_path)
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, f"failed to list {path}: {error}"
            ) from error
        return WorkspaceListResult(tuple(entries), truncated=truncated)

    def search_text(
        self,
        pattern: str,
        path: str | Path,
        *,
        include: str | None = None,
        exclude_dirs: tuple[str, ...] = (),
        max_files: int = 5_000,
        max_matches: int = 200,
    ) -> WorkspaceSearchResult:
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

        base = self.stat_entry(path)
        listing_truncated = False
        if base.is_file:
            files = [base]
        elif base.is_dir:
            excluded = set(exclude_dirs)
            files: list[WorkspaceEntry] = []
            candidate_overflow = False
            simple_include = (
                re.compile(fnmatch_translate(os.path.normcase(include)))
                if include is not None and "/" not in include and "\\" not in include
                else None
            )

            def collect(entry: os.DirEntry[str], relative_path: str) -> None:
                nonlocal candidate_overflow
                if not entry.is_file(follow_symlinks=False):
                    return
                if excluded.intersection(relative_path.split(os.sep)):
                    return
                if include is not None:
                    if simple_include is not None:
                        if (
                            simple_include.fullmatch(os.path.normcase(entry.name))
                            is None
                        ):
                            return
                    elif not Path(relative_path).match(include):
                        return
                if len(files) >= max_files:
                    candidate_overflow = True
                    return
                files.append(self._entry_from_dir_entry(entry, relative_path))

            base_path = self.resolve(path)
            listing_truncated = self._scan_entries(
                base_path,
                include_hidden=True,
                max_entries=max_files * 4,
                visit=collect,
            )
            listing_truncated = listing_truncated or candidate_overflow
        else:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_A_FILE, f"{path} is not searchable"
            )

        matches: list[WorkspaceSearchMatch] = []
        for entry in files:
            try:
                with open(
                    entry.path,
                    "r",
                    encoding="utf-8",
                    errors="replace",
                    newline="",
                ) as stream:
                    text = stream.read()
            except (FileNotFoundError, OSError):
                continue
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

    def glob_paths(
        self,
        pattern: str,
        path: str | Path,
        *,
        max_entries: int = 20_000,
        max_matches: int = 100,
    ) -> WorkspaceGlobResult:
        if max_entries < 1 or max_matches < 1:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH,
                "max_entries and max_matches must be positive",
            )
        base = self.resolve(path)
        if not base.exists():
            raise WorkspaceError(WorkspaceErrorCode.NOT_FOUND, f"{path} not found")
        if not base.is_dir():
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_A_DIRECTORY, f"{path} is not a directory"
            )

        hits: list[WorkspaceEntry] = []
        matcher = compile_portable_glob(pattern)

        def collect(entry: os.DirEntry[str], relative_path: str) -> None:
            if matcher.matches(relative_path):
                hits.append(self._entry_from_dir_entry(entry, relative_path))

        listing_truncated = self._scan_entries(
            base,
            include_hidden=True,
            max_entries=max_entries,
            visit=collect,
        )
        hits.sort(key=lambda entry: entry.mtime, reverse=True)
        return WorkspaceGlobResult(
            entries=tuple(hits[:max_matches]),
            match_count=len(hits),
            listing_truncated=listing_truncated,
        )

    @staticmethod
    def _scan_entries(
        base: Path,
        *,
        include_hidden: bool,
        max_entries: int,
        visit,
    ) -> bool:
        pending = [(os.fspath(base), "")]
        scanned = 0
        try:
            while pending:
                directory, prefix = pending.pop()
                with os.scandir(directory) as iterator:
                    children = sorted(iterator, key=lambda item: item.name.lower())
                for child in children:
                    if not include_hidden and child.name.startswith("."):
                        continue
                    relative_path = (
                        os.path.join(prefix, child.name) if prefix else child.name
                    )
                    visit(child, relative_path)
                    scanned += 1
                    if scanned >= max_entries:
                        return True
                    if child.is_dir(follow_symlinks=False):
                        pending.append((child.path, relative_path))
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR,
                f"failed to scan {base}: {error}",
            ) from error
        return False

    @staticmethod
    def _entry_from_dir_entry(
        entry: os.DirEntry[str], relative_path: str
    ) -> WorkspaceEntry:
        stat_result = entry.stat(follow_symlinks=False)
        return WorkspaceEntry(
            path=entry.path,
            relative_path=relative_path,
            name=entry.name,
            is_file=stat.S_ISREG(stat_result.st_mode),
            is_dir=stat.S_ISDIR(stat_result.st_mode),
            size=stat_result.st_size,
            mtime=stat_result.st_mtime,
            mode=stat_result.st_mode,
        )

    @staticmethod
    def _entry(path: Path, base: Path) -> WorkspaceEntry:
        stat_result = path.stat(follow_symlinks=False)
        return WorkspaceEntry(
            path=str(path),
            relative_path=str(path.relative_to(base)),
            name=path.name,
            is_file=stat.S_ISREG(stat_result.st_mode),
            is_dir=stat.S_ISDIR(stat_result.st_mode),
            size=stat_result.st_size,
            mtime=stat_result.st_mtime,
            mode=stat_result.st_mode,
        )
