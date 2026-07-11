"""Filesystem-backed, root-confined WorkspacePort."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode


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
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND, f"{path} not found"
            )
        if not resolved.is_file():
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_A_FILE, f"{path} is not a file"
            )
        try:
            return resolved.read_text(errors="replace")
        except OSError as error:
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, f"failed to read {path}: {error}"
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
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
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
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND, "old text was not found"
            )
        if occurrences > 1:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_UNIQUE,
                f"old text occurs {occurrences} times",
            )
        updated = content.replace(old, new, 1)
        self.write_text_atomic(path, updated)
        return content, updated
