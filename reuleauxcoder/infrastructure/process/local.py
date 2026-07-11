"""Cancellable local process execution with process-tree cleanup."""

from __future__ import annotations

from collections.abc import Callable
import os
import signal
import subprocess
import threading
import time

from reuleauxcoder.domain.process import ProcessChunk, ProcessResult
from reuleauxcoder.infrastructure.platform import ShellType, get_platform_info


class LocalProcessPort:
    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        cancellation_event: threading.Event | None = None,
        stream_handler: Callable[[ProcessChunk], None] | None = None,
    ) -> ProcessResult:
        if not os.path.isdir(cwd):
            raise FileNotFoundError(f"working directory does not exist ({cwd})")
        platform = get_platform_info()
        shell = platform.get_preferred_shell()
        if platform.is_windows and shell is ShellType.POWERSHELL:
            command = command.replace("&&", ";")
        shell_command = platform.get_shell_executable()
        args: str | list[str]
        use_shell = not bool(shell_command)
        args = command if use_shell else shell_command + [command]

        kwargs = {
            "cwd": cwd,
            "shell": use_shell,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(args, **kwargs)
        deadline = time.monotonic() + timeout
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self._terminate_process_tree(process)
                return ProcessResult(cancelled=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_tree(process)
                return ProcessResult(timed_out=True)
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                if stream_handler is not None:
                    if stdout:
                        stream_handler(ProcessChunk("stdout", stdout))
                    if stderr:
                        stream_handler(ProcessChunk("stderr", stderr))
                return ProcessResult(
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                )
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=1.0)
