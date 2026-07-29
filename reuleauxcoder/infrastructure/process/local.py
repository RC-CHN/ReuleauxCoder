"""Bounded, cancellable local process sessions."""

from __future__ import annotations

from collections.abc import Mapping
import codecs
from dataclasses import dataclass
import errno
import os
import signal
import subprocess
import threading
import time
from typing import Any, Protocol
import uuid

from reuleauxcoder.domain.process import (
    MAX_PROCESS_INPUT_BYTES,
    ProcessCapacityError,
    ProcessChunk,
    ProcessCursor,
    ProcessHandle,
    ProcessOperationUnsupported,
    ProcessResult,
    ProcessSessionNotFound,
    ProcessShutdownReport,
    ProcessSnapshot,
    ProcessState,
    ProcessStreamHandler,
    ProcessStreamMode,
)
from reuleauxcoder.domain.cancellation import CancellationSignal
from reuleauxcoder.infrastructure.platform import get_platform_info
from reuleauxcoder.infrastructure.process.buffer import BoundedTextBuffer


_DEFAULT_RETAINED_BYTES_PER_STREAM = 512 * 1024
_DEFAULT_POLL_BYTES_PER_STREAM = 64 * 1024
_TRAILING_OUTPUT_GRACE_SECONDS = 0.2
_TERMINATE_GRACE_SECONDS = 0.5


def _replace_surrogate_bytes(text: str) -> tuple[str, bool]:
    """Replace only bytes rejected by UTF-8, preserving a real U+FFFD."""
    replaced = any("\udc80" <= character <= "\udcff" for character in text)
    if not replaced:
        return text, False
    return (
        "".join(
            "\ufffd" if "\udc80" <= character <= "\udcff" else character
            for character in text
        ),
        True,
    )


class _PtyTransport(Protocol):
    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def resize(self, rows: int, columns: int) -> None: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


class _FdPtyTransport:
    """Small adapter around a POSIX PTY master descriptor."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd
        self._write_lock = threading.Lock()

    def read(self, size: int) -> bytes:
        fd = self._fd
        if fd is None:
            return b""
        return os.read(fd, size)

    def write(self, data: bytes) -> int:
        with self._write_lock:
            fd = self._fd
            if fd is None:
                raise OSError(errno.EBADF, "PTY is closed")
            written = 0
            while written < len(data):
                count = os.write(fd, data[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "PTY write made no progress")
                written += count
            return written

    def interrupt(self) -> None:
        self.write(b"\x03")

    def resize(self, rows: int, columns: int) -> None:
        import fcntl
        import struct
        import termios

        with self._write_lock:
            fd = self._fd
            if fd is None:
                raise OSError(errno.EBADF, "PTY is closed")
            fcntl.ioctl(
                fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, columns, 0, 0),
            )

    def close(self) -> None:
        with self._write_lock:
            fd = self._fd
            self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class _WinPtyProcessAdapter:
    """Expose pywinpty's ConPTY process through the local Popen subset."""

    stdout = None
    stderr = None

    def __init__(self, process: Any) -> None:
        self._process = process
        self.pid = int(process.pid)

    def wait(self) -> int:
        return int(self._process.wait())

    def poll(self) -> int | None:
        if self._process.isalive():
            return None
        return int(self._process.exitstatus)


class _WinPtyTransport:
    """Transport adapter for a native Windows ConPTY session."""

    def __init__(self, process: Any) -> None:
        self._process = process
        self._lock = threading.Lock()
        self._closed = False

    def read(self, size: int) -> bytes:
        try:
            return str(self._process.read(size)).encode("utf-8")
        except EOFError:
            return b""

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8")
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "ConPTY is closed")
            return int(self._process.write(text))

    def interrupt(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._process.sendcontrol("c")

    def resize(self, rows: int, columns: int) -> None:
        with self._lock:
            if self._closed:
                raise OSError(errno.EBADF, "ConPTY is closed")
            self._process.setwinsize(rows, columns)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._process.close(force=False)
        except (EOFError, OSError):
            pass


class _WindowsJob:
    """Own one Windows process tree until the process session is reaped."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        def windows_error() -> OSError:
            error_code = getattr(ctypes, "get_last_error")()
            return getattr(ctypes, "WinError")(error_code)

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = getattr(ctypes, "WinDLL")(
            "kernel32",
            use_last_error=True,
        )
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise windows_error()
        process_handle = None
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = (
                self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                job,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise windows_error()
            process_handle = kernel32.OpenProcess(
                self._PROCESS_TERMINATE | self._PROCESS_SET_QUOTA,
                False,
                pid,
            )
            if not process_handle:
                raise windows_error()
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise windows_error()
        except BaseException:
            kernel32.CloseHandle(job)
            raise
        finally:
            if process_handle:
                kernel32.CloseHandle(process_handle)

        self._kernel32 = kernel32
        self._handle: int | None = int(job)
        self._lock = threading.Lock()

    def terminate(self) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            if not self._kernel32.TerminateJobObject(handle, 1):
                import ctypes

                error_code = getattr(ctypes, "get_last_error")()
                raise getattr(ctypes, "WinError")(error_code)

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


class _LocalProcessEntry:
    def __init__(
        self,
        *,
        session_id: str,
        process: Any,
        stream_mode: ProcessStreamMode,
        runtime_timeout: int,
        stream_handler: ProcessStreamHandler | None,
        retained_bytes_per_stream: int,
        pty_transport: _PtyTransport | None = None,
    ) -> None:
        self.session_id = session_id
        self.process = process
        self.stream_mode = stream_mode
        self.runtime_timeout = runtime_timeout
        self.stream_handler = stream_handler
        self.pty_transport = pty_transport
        self.process_tree_owner: _WindowsJob | None = None
        self.stdout = BoundedTextBuffer(retained_bytes_per_stream)
        self.stderr = BoundedTextBuffer(retained_bytes_per_stream)
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.state = ProcessState.RUNNING
        self.exit_code: int | None = None
        self.termination_reason: str | None = None
        self.output_truncated = False
        self.output_decode_replaced = False
        self.interrupt_requested = False
        self.termination_requested = False
        self.termination_lock = threading.Lock()
        self.condition = threading.Condition(threading.RLock())
        self.done = threading.Event()
        self.reader_threads: list[threading.Thread] = []
        self.control_threads: list[threading.Thread] = []
        self.auto_release = False

    def append(
        self,
        stream: str,
        text: str,
        byte_count: int,
        *,
        decode_replaced: bool = False,
    ) -> None:
        target = self.stdout if stream == "stdout" else self.stderr
        target.append(text, byte_count=byte_count)
        with self.condition:
            self.output_decode_replaced = (
                self.output_decode_replaced or decode_replaced
            )
            self.condition.notify_all()
        handler = self.stream_handler
        if handler is not None and text:
            try:
                handler(ProcessChunk(stream, text))
            except Exception:
                pass


@dataclass(slots=True)
class _LocalStartReservation:
    done: threading.Event
    handle: ProcessHandle | None = None
    error: BaseException | None = None


class LocalProcessPort:
    """Own local subprocesses independently from individual tool calls."""

    backend_name = "local"

    def __init__(
        self,
        *,
        retained_bytes_per_stream: int = _DEFAULT_RETAINED_BYTES_PER_STREAM,
        poll_bytes_per_stream: int = _DEFAULT_POLL_BYTES_PER_STREAM,
        max_sessions: int = 64,
    ) -> None:
        if retained_bytes_per_stream < 1:
            raise ValueError("retained_bytes_per_stream must be positive")
        if poll_bytes_per_stream < 1:
            raise ValueError("poll_bytes_per_stream must be positive")
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self._retained_bytes_per_stream = retained_bytes_per_stream
        self._poll_bytes_per_stream = poll_bytes_per_stream
        self._max_sessions = max_sessions
        self._entries: dict[str, _LocalProcessEntry] = {}
        self._idempotency: dict[str, str] = {}
        self._starting: dict[str, _LocalStartReservation] = {}
        self._lock = threading.RLock()
        self._closing = False

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
    ) -> ProcessHandle:
        if not isinstance(command, str) or not command:
            raise ValueError("command must be a non-empty string")
        if not isinstance(runtime_timeout, int) or isinstance(runtime_timeout, bool):
            raise ValueError("runtime_timeout must be a positive integer")
        if runtime_timeout < 1:
            raise ValueError("runtime_timeout must be a positive integer")
        if not os.path.isdir(cwd):
            raise FileNotFoundError(f"working directory does not exist ({cwd})")

        key = idempotency_key or uuid.uuid4().hex
        while True:
            with self._lock:
                if self._closing:
                    raise RuntimeError("process port is shutting down")
                existing_id = self._idempotency.get(key)
                if existing_id is not None and existing_id in self._entries:
                    existing = self._entries[existing_id]
                    return ProcessHandle(existing.session_id, existing.stream_mode)
                pending = self._starting.get(key)
                if pending is None:
                    if len(self._entries) + len(self._starting) >= self._max_sessions:
                        running = sum(
                            entry.state is not ProcessState.EXITED
                            for entry in self._entries.values()
                        )
                        raise ProcessCapacityError(
                            "local process capacity reached "
                            f"(limit={self._max_sessions}, running={running}, "
                            f"retained={len(self._entries) - running}, "
                            f"starting={len(self._starting)})"
                        )
                    reservation = _LocalStartReservation(threading.Event())
                    self._starting[key] = reservation
                    break
            pending.done.wait()
            if pending.handle is not None:
                return pending.handle
            if pending.error is not None:
                raise RuntimeError(
                    f"idempotent process start failed: {pending.error}"
                ) from pending.error

        entry: _LocalProcessEntry | None = None
        registered = False
        watcher_started = False
        result_handle: ProcessHandle | None = None
        start_error: BaseException | None = None
        try:
            invocation = get_platform_info().resolve_shell_invocation(command, tty=tty)
            session_id = f"proc_{uuid.uuid4().hex}"
            if tty:
                process, pty_transport = self._spawn_pty(
                    invocation.argv,
                    cwd=cwd,
                    env=env,
                )
                mode = ProcessStreamMode.PTY
            else:
                process = self._spawn_pipe(invocation.argv, cwd=cwd, env=env)
                pty_transport = None
                mode = ProcessStreamMode.PIPE

            entry = _LocalProcessEntry(
                session_id=session_id,
                process=process,
                stream_mode=mode,
                runtime_timeout=runtime_timeout,
                stream_handler=stream_handler,
                retained_bytes_per_stream=self._retained_bytes_per_stream,
                pty_transport=pty_transport,
            )
            if os.name == "nt":
                entry.process_tree_owner = _WindowsJob(int(process.pid))
            with self._lock:
                if self._closing:
                    reject_start = True
                else:
                    reject_start = False
                    self._entries[session_id] = entry
                    self._idempotency[key] = session_id
                    registered = True
            if reject_start:
                raise RuntimeError("process port is shutting down")

            self._start_readers(entry)
            watcher = threading.Thread(
                target=self._watch_process,
                args=(entry, key),
                name=f"rcoder-process-watch-{session_id[-8:]}",
                daemon=True,
            )
            entry.control_threads.append(watcher)
            watcher.start()
            watcher_started = True
            deadline = threading.Thread(
                target=self._watch_deadline,
                args=(entry,),
                name=f"rcoder-process-deadline-{session_id[-8:]}",
                daemon=True,
            )
            entry.control_threads.append(deadline)
            deadline.start()
            result_handle = ProcessHandle(session_id, mode)
            return result_handle
        except BaseException as error:
            start_error = error
            if registered and entry is not None:
                self._remove_entry(entry.session_id, idempotency_key=key)
            if entry is not None:
                self._cleanup_failed_start(
                    entry,
                    watcher_started=watcher_started,
                )
            raise
        finally:
            with self._lock:
                if self._starting.get(key) is reservation:
                    self._starting.pop(key, None)
                    reservation.handle = result_handle
                    reservation.error = start_error
                    reservation.done.set()

    @staticmethod
    def _spawn_pipe(
        argv: tuple[str, ...],
        *,
        cwd: str,
        env: Mapping[str, str] | None,
    ) -> subprocess.Popen[bytes]:
        kwargs: dict[str, object] = {
            "cwd": cwd,
            "env": dict(env) if env is not None else None,
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": False,
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(list(argv), **kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _spawn_pty(
        argv: tuple[str, ...],
        *,
        cwd: str,
        env: Mapping[str, str] | None,
    ) -> tuple[Any, _PtyTransport]:
        if os.name == "nt":
            try:
                from winpty import PtyProcess
            except ImportError as error:
                raise ProcessOperationUnsupported(
                    "PTY requires the pywinpty ConPTY runtime on Windows"
                ) from error
            process = PtyProcess.spawn(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                dimensions=(24, 80),
            )
            return _WinPtyProcessAdapter(process), _WinPtyTransport(process)
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                slave_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", 24, 80, 0, 0),
            )
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                shell=False,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                text=False,
                bufsize=0,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return process, _FdPtyTransport(master_fd)

    def _start_readers(self, entry: _LocalProcessEntry) -> None:
        if entry.stream_mode is ProcessStreamMode.PTY:
            assert entry.pty_transport is not None
            reader = threading.Thread(
                target=self._read_pty,
                args=(entry, entry.pty_transport, "stdout"),
                name=f"rcoder-process-pty-{entry.session_id[-8:]}",
                daemon=True,
            )
            entry.reader_threads.append(reader)
            reader.start()
            return

        assert entry.process.stdout is not None
        assert entry.process.stderr is not None
        for stream, pipe in (
            ("stdout", entry.process.stdout),
            ("stderr", entry.process.stderr),
        ):
            reader = threading.Thread(
                target=self._read_pipe,
                args=(entry, pipe, stream),
                name=f"rcoder-process-{stream}-{entry.session_id[-8:]}",
                daemon=True,
            )
            entry.reader_threads.append(reader)
            reader.start()

    @staticmethod
    def _read_pipe(entry: _LocalProcessEntry, pipe, stream: str) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
        try:
            while True:
                data = pipe.read(8192)
                if not data:
                    break
                text, replaced = _replace_surrogate_bytes(
                    decoder.decode(data, final=False)
                )
                entry.append(
                    stream,
                    text,
                    len(data),
                    decode_replaced=replaced,
                )
            tail, replaced = _replace_surrogate_bytes(
                decoder.decode(b"", final=True)
            )
            if tail:
                entry.append(
                    stream,
                    tail,
                    0,
                    decode_replaced=replaced,
                )
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _read_pty(
        entry: _LocalProcessEntry,
        transport: _PtyTransport,
        stream: str,
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
        try:
            while True:
                try:
                    data = transport.read(8192)
                except OSError as error:
                    if error.errno in {errno.EIO, errno.EBADF}:
                        break
                    raise
                if not data:
                    break
                text, replaced = _replace_surrogate_bytes(
                    decoder.decode(data, final=False)
                )
                entry.append(
                    stream,
                    text,
                    len(data),
                    decode_replaced=replaced,
                )
            tail, replaced = _replace_surrogate_bytes(
                decoder.decode(b"", final=True)
            )
            if tail:
                entry.append(
                    stream,
                    tail,
                    0,
                    decode_replaced=replaced,
                )
        except OSError:
            pass

    def _watch_deadline(self, entry: _LocalProcessEntry) -> None:
        if entry.done.wait(entry.runtime_timeout):
            return
        self._request_termination(entry, reason="timeout")

    def _watch_process(self, entry: _LocalProcessEntry, idempotency_key: str) -> None:
        try:
            exit_code = entry.process.wait()
        except Exception:
            exit_code = -1

        for reader in entry.reader_threads:
            reader.join(timeout=_TRAILING_OUTPUT_GRACE_SECONDS)
        readers_alive = any(reader.is_alive() for reader in entry.reader_threads)
        if os.name != "nt" or readers_alive:
            # Permanent detach is not part of the process-session contract. On
            # POSIX the original process group catches descendants whether they
            # inherited a stream or redirected every stream away from rcoder.
            self._signal_tree(entry, force=True, include_exited_group=True)
        if readers_alive:
            self._close_entry_streams(entry)
            for reader in entry.reader_threads:
                reader.join(timeout=0.1)

        with entry.condition:
            entry.exit_code = exit_code
            if entry.termination_reason is None:
                if entry.interrupt_requested and exit_code in {
                    -getattr(signal, "SIGINT", 2),
                    130,
                }:
                    entry.termination_reason = "interrupted"
                else:
                    entry.termination_reason = "exit"
            entry.finished_at = time.time()
            entry.state = ProcessState.EXITED
            entry.done.set()
            entry.condition.notify_all()
        self._close_entry_streams(entry)
        if entry.auto_release:
            self._remove_entry(entry.session_id, idempotency_key=idempotency_key)

    @staticmethod
    def _close_entry_streams(entry: _LocalProcessEntry) -> None:
        if entry.process_tree_owner is not None:
            entry.process_tree_owner.close()
            entry.process_tree_owner = None
        if entry.pty_transport is not None:
            entry.pty_transport.close()
            entry.pty_transport = None
        for pipe in (entry.process.stdout, entry.process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

    def _cleanup_failed_start(
        self,
        entry: _LocalProcessEntry,
        *,
        watcher_started: bool,
    ) -> None:
        """Synchronously reclaim a process that was never exposed to callers."""
        self._signal_tree(entry, force=True)
        if watcher_started:
            entry.done.wait(2.0)
        else:
            self._close_entry_streams(entry)
            try:
                entry.process.wait(timeout=2.0)
            except TypeError:
                entry.process.wait()
            except (OSError, subprocess.TimeoutExpired):
                self._signal_tree(entry, force=True, include_exited_group=True)
                try:
                    entry.process.wait(timeout=1.0)
                except TypeError:
                    entry.process.wait()
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self._close_entry_streams(entry)
        for reader in entry.reader_threads:
            reader.join(timeout=0.2)

    def poll(
        self,
        session_id: str,
        *,
        cursor: ProcessCursor | None = None,
        wait_ms: int = 0,
    ) -> ProcessSnapshot:
        if wait_ms < 0:
            raise ValueError("wait_ms must be non-negative")
        entry = self._lookup(session_id)
        current = cursor or ProcessCursor()
        deadline = time.monotonic() + (wait_ms / 1000)
        with entry.condition:
            while (
                entry.state is ProcessState.RUNNING
                and entry.stdout.end_offset <= current.stdout_offset
                and entry.stderr.end_offset <= current.stderr_offset
                and wait_ms > 0
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                entry.condition.wait(timeout=remaining)
        return self._snapshot(entry, current)

    def _snapshot(
        self,
        entry: _LocalProcessEntry,
        cursor: ProcessCursor,
    ) -> ProcessSnapshot:
        stdout = entry.stdout.read_after(
            cursor.stdout_offset,
            max_bytes=self._poll_bytes_per_stream,
        )
        stderr = entry.stderr.read_after(
            cursor.stderr_offset,
            max_bytes=self._poll_bytes_per_stream,
        )
        with entry.condition:
            entry.output_truncated = (
                entry.output_truncated
                or stdout.truncated
                or stderr.truncated
                or entry.stdout.truncated
                or entry.stderr.truncated
            )
            return ProcessSnapshot(
                session_id=entry.session_id,
                state=entry.state,
                stream_mode=entry.stream_mode,
                backend=self.backend_name,
                stdout=stdout.text,
                stderr=stderr.text,
                cursor=ProcessCursor(stdout.next_offset, stderr.next_offset),
                exit_code=entry.exit_code,
                termination_reason=(
                    entry.termination_reason
                    if entry.state is ProcessState.EXITED
                    else None
                ),
                started_at=entry.started_at,
                finished_at=entry.finished_at,
                runtime_timeout_seconds=entry.runtime_timeout,
                output_truncated=entry.output_truncated,
                output_decode_replaced=entry.output_decode_replaced,
                total_stdout_bytes=entry.stdout.total_bytes,
                total_stderr_bytes=entry.stderr.total_bytes,
            )

    def write_input(self, session_id: str, data: str) -> int:
        if not isinstance(data, str) or not data:
            raise ValueError("data must be a non-empty string")
        entry = self._lookup(session_id)
        if entry.stream_mode is not ProcessStreamMode.PTY:
            raise ProcessOperationUnsupported(
                f"session '{session_id}' uses pipe mode; stdin is closed"
            )
        if entry.state is not ProcessState.RUNNING or entry.pty_transport is None:
            raise ProcessOperationUnsupported(f"session '{session_id}' has exited")
        encoded = data.encode("utf-8")
        if len(encoded) > MAX_PROCESS_INPUT_BYTES:
            raise ProcessCapacityError(
                "process input exceeds the 64 KiB per-write limit"
            )
        transport = entry.pty_transport
        if transport is None:
            raise ProcessOperationUnsupported(f"session '{session_id}' has exited")
        return transport.write(encoded)

    def resize(self, session_id: str, *, rows: int, columns: int) -> None:
        if rows < 1 or columns < 1:
            raise ValueError("terminal rows and columns must be positive")
        entry = self._lookup(session_id)
        if entry.stream_mode is not ProcessStreamMode.PTY:
            raise ProcessOperationUnsupported(
                f"session '{session_id}' uses pipe mode; terminal size is unavailable"
            )
        with entry.condition:
            transport = entry.pty_transport
            if entry.state is not ProcessState.RUNNING or transport is None:
                raise ProcessOperationUnsupported(
                    f"session '{session_id}' has exited"
                )
        transport.resize(rows, columns)

    def interrupt(self, session_id: str) -> ProcessSnapshot:
        entry = self._lookup(session_id)
        with entry.condition:
            if entry.state is ProcessState.EXITED:
                return self._snapshot(entry, ProcessCursor())
            if entry.interrupt_requested or entry.termination_requested:
                return self._snapshot(entry, ProcessCursor())
            try:
                if entry.stream_mode is ProcessStreamMode.PTY:
                    if entry.pty_transport is None:
                        raise OSError(errno.EBADF, "PTY is closed")
                    entry.pty_transport.interrupt()
                elif os.name == "nt":
                    entry.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(entry.process.pid, signal.SIGINT)
            except ProcessLookupError as error:
                if entry.process.poll() is not None:
                    entry.done.wait(0.1)
                    return self._snapshot(entry, ProcessCursor())
                raise ProcessOperationUnsupported(
                    f"interrupt was not delivered to session '{session_id}': {error}"
                ) from error
            except (OSError, ValueError, EOFError) as error:
                raise ProcessOperationUnsupported(
                    f"interrupt was not delivered to session '{session_id}': {error}"
                ) from error
            entry.interrupt_requested = True
        return self._snapshot(entry, ProcessCursor())

    def terminate(
        self,
        session_id: str,
        *,
        reason: str = "terminated",
    ) -> ProcessSnapshot:
        entry = self._lookup(session_id)
        self._request_termination(
            entry,
            reason=reason,
            report_delivery_failure=True,
        )
        return self._snapshot(entry, ProcessCursor())

    def _request_termination(
        self,
        entry: _LocalProcessEntry,
        *,
        reason: str,
        report_delivery_failure: bool = False,
    ) -> None:
        with entry.termination_lock:
            with entry.condition:
                if entry.state is ProcessState.EXITED:
                    return
                previous_reason = entry.termination_reason
                if entry.termination_reason is None or reason in {
                    "timeout",
                    "shutdown",
                }:
                    entry.termination_reason = reason
                if entry.termination_requested:
                    return
                entry.termination_requested = True
            try:
                self._signal_tree(entry, force=False)
            except OSError as error:
                if report_delivery_failure:
                    with entry.condition:
                        entry.termination_requested = False
                        entry.termination_reason = previous_reason
                    raise ProcessOperationUnsupported(
                        "termination was not delivered to session "
                        f"'{entry.session_id}': {error}"
                    ) from error

            def escalate() -> None:
                if entry.done.wait(_TERMINATE_GRACE_SECONDS):
                    return
                try:
                    self._signal_tree(entry, force=True)
                except OSError:
                    return

            reaper = threading.Thread(
                target=escalate,
                name=f"rcoder-process-reaper-{entry.session_id[-8:]}",
                daemon=True,
            )
            entry.control_threads.append(reaper)
            reaper.start()

    @staticmethod
    def _signal_tree(
        entry: _LocalProcessEntry,
        *,
        force: bool,
        include_exited_group: bool = False,
    ) -> None:
        process = entry.process
        if process.poll() is not None and not include_exited_group:
            return
        if os.name == "nt":
            owner = entry.process_tree_owner
            if owner is not None:
                try:
                    owner.terminate()
                    return
                except OSError:
                    pass
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0 and process.poll() is None:
                detail = completed.stderr.decode(errors="replace").strip()
                raise OSError(detail or "taskkill did not confirm process termination")
            return
        selected = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, selected)
        except ProcessLookupError:
            pass

    def release(self, session_id: str) -> None:
        entry = self._lookup(session_id)
        if entry.state is ProcessState.RUNNING:
            raise RuntimeError(f"cannot release running process session '{session_id}'")
        self._remove_entry(session_id)

    def _lookup(self, session_id: str) -> _LocalProcessEntry:
        with self._lock:
            entry = self._entries.get(session_id)
        if entry is None:
            raise ProcessSessionNotFound(f"process session '{session_id}' was not found")
        return entry

    def _remove_entry(
        self,
        session_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        with self._lock:
            self._entries.pop(session_id, None)
            if idempotency_key is not None:
                self._idempotency.pop(idempotency_key, None)
            else:
                stale = [
                    key for key, value in self._idempotency.items() if value == session_id
                ]
                for key in stale:
                    self._idempotency.pop(key, None)

    def shutdown(self, *, grace_seconds: float = 0.5) -> ProcessShutdownReport:
        with self._lock:
            self._closing = True
            entries = tuple(self._entries.values())
            starting = tuple(
                reservation.done for reservation in self._starting.values()
            )
        start_deadline = time.monotonic() + 2.0
        start_reap_timeouts = 0
        for reservation in starting:
            if not reservation.wait(max(0.0, start_deadline - time.monotonic())):
                start_reap_timeouts += 1
        already_exited = sum(
            entry.state is ProcessState.EXITED for entry in entries
        )
        live = [entry for entry in entries if entry.state is ProcessState.RUNNING]
        for entry in live:
            try:
                self.interrupt(entry.session_id)
            except ProcessSessionNotFound:
                pass
        deadline = time.monotonic() + max(0.0, grace_seconds)
        for entry in live:
            entry.done.wait(max(0.0, deadline - time.monotonic()))
        remaining = [entry for entry in live if not entry.done.is_set()]
        for entry in remaining:
            self._request_termination(entry, reason="shutdown")
        reap_timeouts = start_reap_timeouts
        reap_deadline = time.monotonic() + 2.0
        for entry in remaining:
            if not entry.done.wait(
                max(0.0, reap_deadline - time.monotonic())
            ):
                reap_timeouts += 1
        with self._lock:
            self._entries.clear()
            self._idempotency.clear()
        return ProcessShutdownReport(
            total=len(entries),
            already_exited=already_exited,
            interrupted=len(live),
            terminated=len(remaining),
            reap_timeouts=reap_timeouts,
        )

    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        cancellation_event: CancellationSignal | None = None,
        stream_handler: ProcessStreamHandler | None = None,
    ) -> ProcessResult:
        handle = self.start(
            command,
            cwd=cwd,
            runtime_timeout=timeout,
            tty=False,
            stream_handler=stream_handler,
        )
        entry = self._lookup(handle.session_id)
        cursor = ProcessCursor()
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self._request_termination(entry, reason="cancelled")
                stdout = entry.stdout.retained()
                stderr = entry.stderr.retained()
                with entry.condition:
                    if entry.done.is_set():
                        self._remove_entry(handle.session_id)
                    else:
                        entry.auto_release = True
                return ProcessResult(
                    stdout=stdout.text,
                    stderr=stderr.text,
                    exit_code=entry.process.poll(),
                    cancelled=True,
                    output_truncated=stdout.truncated or stderr.truncated,
                    output_decode_replaced=entry.output_decode_replaced,
                )
            snapshot = self.poll(
                handle.session_id,
                cursor=cursor,
                wait_ms=50,
            )
            cursor = snapshot.cursor
            if snapshot.state is ProcessState.RUNNING:
                continue
            stdout = entry.stdout.retained()
            stderr = entry.stderr.retained()
            result = ProcessResult(
                stdout=stdout.text,
                stderr=stderr.text,
                exit_code=snapshot.exit_code,
                timed_out=snapshot.termination_reason == "timeout",
                cancelled=snapshot.termination_reason == "cancelled",
                output_truncated=stdout.truncated or stderr.truncated,
                output_decode_replaced=snapshot.output_decode_replaced,
            )
            self.release(handle.session_id)
            return result
