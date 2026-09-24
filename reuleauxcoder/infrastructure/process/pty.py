"""Platform PTY transports; data backpressure cannot own control state."""

import errno
import os
import select
import threading
import time
from typing import Any, Protocol

_INPUT_WAIT_SECONDS = 0.25


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
        self._io_lock = threading.Lock()
        os.set_blocking(fd, False)

    def read(self, size: int) -> bytes:
        while True:
            with self._io_lock:
                fd = self._fd
                if fd is None:
                    return b""
                try:
                    return os.read(fd, size)
                except BlockingIOError:
                    pass
            # Descriptor ownership is rechecked before every syscall. A closed
            # descriptor may be reused by another session while select waits.
            try:
                select.select([fd], [], [], 0.05)
            except (OSError, ValueError):
                continue

    def write(self, data: bytes) -> int:
        deadline = time.monotonic() + _INPUT_WAIT_SECONDS
        if not self._write_lock.acquire(timeout=_INPUT_WAIT_SECONDS):
            raise TimeoutError("PTY input is busy; no input was sent")
        try:
            written = 0
            while written < len(data):
                with self._io_lock:
                    fd = self._fd
                    if fd is None:
                        if written:
                            return written
                        raise OSError(errno.EBADF, "PTY is closed")
                    try:
                        count = os.write(fd, data[written:])
                    except BlockingIOError:
                        count = 0
                    except OSError:
                        if written:
                            return written
                        raise
                written += count
                remaining = deadline - time.monotonic()
                if written == len(data) or remaining <= 0:
                    return written
                if count == 0:
                    try:
                        select.select([], [fd], [], min(remaining, 0.05))
                    except (OSError, ValueError):
                        continue
            return written
        finally:
            self._write_lock.release()

    def interrupt(self) -> None:
        # Control input never queues behind the user's input writer.
        with self._io_lock:
            if self._fd is None:
                raise OSError(errno.EBADF, "PTY is closed")
            os.write(self._fd, b"\x03")

    def resize(self, rows: int, columns: int) -> None:
        import fcntl
        import struct
        import termios

        with self._io_lock:
            if self._fd is None:
                raise OSError(errno.EBADF, "PTY is closed")
            fcntl.ioctl(
                self._fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, columns, 0, 0),
            )

    def close(self) -> None:
        with self._io_lock:
            fd, self._fd = self._fd, None
            if fd is not None:
                os.close(fd)


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

