"""Platform-neutral process session contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from reuleauxcoder.domain.cancellation import CancellationSignal

MAX_PROCESS_INPUT_BYTES = 64 * 1024
MAX_PROCESS_SESSION_INPUT_BYTES = 1024 * 1024


class ProcessState(str, Enum):
    """Small model-facing process state space."""

    RUNNING = "running"
    EXITED = "exited"
    UNKNOWN = "unknown"


class ProcessStreamMode(str, Enum):
    """How process output and input are transported."""

    PIPE = "pipe"
    PTY = "pty"


@dataclass(frozen=True, slots=True)
class ProcessChunk:
    stream: str
    data: str


ProcessStreamHandler = Callable[[ProcessChunk], None]


@dataclass(frozen=True, slots=True)
class ProcessCursor:
    """Opaque-to-model monotonic offsets for one process consumer."""

    stdout_offset: int = 0
    stderr_offset: int = 0


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    """Stable handle returned after a process has been accepted."""

    session_id: str
    stream_mode: ProcessStreamMode


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """One bounded incremental observation of a process session."""

    session_id: str
    state: ProcessState
    stream_mode: ProcessStreamMode
    backend: str
    stdout: str = ""
    stderr: str = ""
    cursor: ProcessCursor = ProcessCursor()
    exit_code: int | None = None
    termination_reason: str | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    runtime_timeout_seconds: int = 0
    output_truncated: bool = False
    output_decode_replaced: bool = False
    total_stdout_bytes: int = 0
    total_stderr_bytes: int = 0

    @property
    def elapsed_seconds(self) -> float:
        import time

        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Legacy blocking process result retained during migration."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False
    output_decode_replaced: bool = False
    state_unknown: bool = False


@dataclass(frozen=True, slots=True)
class ProcessShutdownReport:
    """Bounded cleanup facts for one process port or manager.

    interrupted and terminated count control requests sent during cleanup;
    they do not claim that an unresolved remote process acknowledged the action.
    """

    total: int = 0
    already_exited: int = 0
    interrupted: int = 0
    terminated: int = 0
    unknown: int = 0
    reap_timeouts: int = 0


class ProcessSessionError(RuntimeError):
    """Base error for process-session operations."""


class ProcessSessionNotFound(ProcessSessionError):
    """The supplied opaque session identifier is not owned by this port."""


class ProcessOperationUnsupported(ProcessSessionError):
    """The requested operation is not available for this process session."""


class ProcessOperationUnconfirmed(ProcessSessionError):
    """The operation may have been delivered, but its result is ambiguous."""

    def __init__(
        self,
        message: str,
        *,
        snapshot: ProcessSnapshot | None = None,
    ) -> None:
        super().__init__(message)
        self.snapshot = snapshot


class ProcessCapacityError(ProcessSessionError):
    """The process session capacity is exhausted."""


class ProcessPort(Protocol):
    """Transport/platform primitive used by the session-level manager."""

    backend_name: str

    def start(
        self,
        command: str,
        *,
        cwd: str,
        runtime_timeout: int,
        tty: bool = False,
        env: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        stream_handler: ProcessStreamHandler | None = None,
    ) -> ProcessHandle: ...

    def poll(
        self,
        session_id: str,
        *,
        cursor: ProcessCursor | None = None,
        wait_ms: int = 0,
    ) -> ProcessSnapshot: ...

    def write_input(self, session_id: str, data: str) -> int: ...

    def resize(self, session_id: str, *, rows: int, columns: int) -> None: ...

    def interrupt(self, session_id: str) -> ProcessSnapshot: ...

    def terminate(
        self,
        session_id: str,
        *,
        reason: str = "terminated",
    ) -> ProcessSnapshot: ...

    def release(self, session_id: str) -> None: ...

    def shutdown(self, *, grace_seconds: float = 0.5) -> ProcessShutdownReport: ...

    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        cancellation_event: CancellationSignal | None = None,
        stream_handler: ProcessStreamHandler | None = None,
    ) -> ProcessResult: ...
