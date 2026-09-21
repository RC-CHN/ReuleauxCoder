"""Exercise the shipped launcher in an actual PTY, including resize and teardown."""

import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time


package = Path(__file__).resolve().parent.parent
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 90, 0, 0))
capture = bytearray()
ansi = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")


def read_until(value, timeout=10):
    start = len(capture)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([master], [], [], 0.05)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            capture.extend(chunk)
        if value.encode() in ansi.sub(b"", bytes(capture[start:])):
            return
    raise AssertionError(
        f"Missing terminal output: {value}\n"
        + ansi.sub(b"", bytes(capture)).decode(errors="replace")
    )


with tempfile.TemporaryDirectory(prefix="rcoder-tui-pty-") as cwd:
    command = (
        [sys.argv[2]]
        if sys.argv[1] == "--launcher"
        else [
            sys.argv[1],
            sys.argv[2] if len(sys.argv) > 2 else str(package / "dist/cli.js"),
        ]
    )
    child = subprocess.Popen(
        [
            *command,
            "--backend",
            sys.executable,
            "--",
            str(package / "test/backend.py"),
        ],
        cwd=cwd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color", "FORCE_COLOR": "1"},
    )
    os.close(slave)
    try:
        read_until("Ready")
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 140, 0, 0))
        child.send_signal(signal.SIGWINCH)
        read_until("WORKBENCH")
        os.write(master, b"/model")
        read_until("Commands")
        os.write(master, b"\r")
        read_until("View all details")
        os.write(master, b"\x1b")
        time.sleep(0.1)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 14, 50, 0, 0))
        child.send_signal(signal.SIGWINCH)
        os.write(master, "\x1b[200~中文任务\x1b[201~".encode())
        read_until("中文任务")
        os.write(master, b"\x1b[<64;10;5M\x1b[<65;10;5M")
        os.write(master, b"\r")
        read_until("successfully")
        os.write(master, b"\x04")
        read_until("Saved session:")
        assert child.wait(timeout=10) == 0
        assert b"\x1b[?1049h" in capture
        assert b"\x1b[?1049l" in capture
        assert b"\x1b[?1007l" in capture
        assert b"\x1b[?1000h" in capture and b"\x1b[?1006h" in capture
        assert b"\x1b[?1000l" in capture and b"\x1b[?1006l" in capture
        assert Path(cwd, ".rcoder/tui-history.jsonl").read_text() == '"中文任务"\n'
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
print("PTY launcher, menus, Unicode, resize, save and terminal restoration passed")
