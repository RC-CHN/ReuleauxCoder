"""Filesystem-backed, root-confined WorkspacePort."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile

from reuleauxcoder.domain.workspace import (
    WorkspaceEntry,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceListResult,
    WorkspaceSearchResult,
    search_text_via_primitives,
)


class LocalWorkspacePort:
    def __init__(self, root: str | Path, *, cwd: str | Path | None = None):
        self.root = Path(root).expanduser().resolve()
        self.cwd = Path(cwd).expanduser().resolve() if cwd is not None else self.root
        try:
            self.cwd.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"cwd escapes workspace root: {self.cwd}",
            ) from error

    def resolve(self, path: str | Path) -> Path:
        if not isinstance(path, (str, Path)) or not str(path):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "path must be a non-empty string"
            )
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"path escapes workspace root: {path}",
            ) from error
        return resolved

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

    def write_text_atomic(self, path: str | Path, content: str) -> str:
        if not isinstance(content, str):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "content must be a string"
            )
        resolved = self.resolve(path)
        old = self.read_text(resolved) if resolved.exists() else ""
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{resolved.name}.", dir=resolved.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if resolved.exists():
                    os.chmod(temporary, resolved.stat().st_mode)
                os.replace(temporary, resolved)
            except BaseException:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
        except WorkspaceError:
            raise
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, f"failed to write {path}: {error}"
            ) from error
        return old

    def replace_exact_atomic(
        self, path: str | Path, old: str, new: str
    ) -> tuple[str, str]:
        if not isinstance(old, str) or not isinstance(new, str):
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH,
                "old and new values must be strings",
            )
        if old == new:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_PATH, "old and new values must differ"
            )
        content = self.read_text(path)
        occurrences = content.count(old)
        if occurrences == 0:
            raise WorkspaceError(WorkspaceErrorCode.NOT_FOUND, "old text was not found")
        if occurrences > 1:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_UNIQUE,
                f"old text occurs {occurrences} times",
            )
        updated = content.replace(old, new, 1)
        self.write_text_atomic(path, updated)
        return content, updated

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
        return search_text_via_primitives(
            self,
            pattern,
            path,
            include=include,
            exclude_dirs=exclude_dirs,
            max_files=max_files,
            max_matches=max_matches,
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
