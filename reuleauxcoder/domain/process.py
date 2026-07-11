"""Platform-neutral process execution primitive contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import threading
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProcessChunk:
    stream: str
    data: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False


class ProcessPort(Protocol):
    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        cancellation_event: threading.Event | None = None,
        stream_handler: Callable[[ProcessChunk], None] | None = None,
    ) -> ProcessResult: ...
