"""Platform-neutral workspace primitive contract."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
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


class WorkspacePort(Protocol):
    root: Path

    def resolve(self, path: str | Path) -> Path: ...

    def read_text(self, path: str | Path) -> str: ...

    def write_text_atomic(self, path: str | Path, content: str) -> str: ...

    def replace_exact_atomic(
        self, path: str | Path, old: str, new: str
    ) -> tuple[str, str]: ...
