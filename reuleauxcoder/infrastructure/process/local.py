"""Cancellable local process execution with process-tree cleanup."""

from __future__ import annotations

from collections.abc import Callable
import os
import queue
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
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(args, **kwargs)
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        chunk_queue: queue.Queue[ProcessChunk] = queue.Queue()

        def read_stream(pipe, stream: str, parts: list[str]) -> None:
            try:
                for chunk in iter(pipe.readline, ""):
                    parts.append(chunk)
                    chunk_queue.put(ProcessChunk(stream, chunk))
            finally:
                pipe.close()

        readers = [
            threading.Thread(
                target=read_stream,
                args=(process.stdout, "stdout", stdout_parts),
                daemon=True,
            ),
            threading.Thread(
                target=read_stream,
                args=(process.stderr, "stderr", stderr_parts),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()

        def drain_chunks() -> None:
            while True:
                try:
                    chunk = chunk_queue.get_nowait()
                except queue.Empty:
                    return
                if stream_handler is not None:
                    stream_handler(chunk)

        def finish_result(**state) -> ProcessResult:
            for reader in readers:
                reader.join(timeout=1.0)
            drain_chunks()
            return ProcessResult(
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                exit_code=process.returncode,
                **state,
            )

        deadline = time.monotonic() + timeout
        while True:
            drain_chunks()
            if cancellation_event is not None and cancellation_event.is_set():
                self._terminate_process_tree(process)
                return finish_result(cancelled=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_tree(process)
                return finish_result(timed_out=True)
            if process.poll() is not None:
                return finish_result()
            if cancellation_event is not None:
                cancellation_event.wait(timeout=min(0.05, remaining))
            else:
                time.sleep(min(0.05, remaining))

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
