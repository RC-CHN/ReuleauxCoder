"""Shell command execution with safety checks."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.infrastructure.platform import ShellType, get_platform_info


class ShellCancelled(RuntimeError):
    pass


@register_tool
class ShellTool(Tool):
    name = "shell"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory for this command.  Defaults to the "
                    "session's current working directory (set by a previous "
                    "persist_cwd, or the project root on startup).  Use this "
                    "to run a one-off command elsewhere without switching the "
                    "session directory."
                ),
            },
            "persist_cwd": {
                "type": "boolean",
                "description": (
                    "If true, update the session's default working directory "
                    "to the provided cwd.  Subsequent shell calls without an "
                    "explicit cwd will use this new directory.  Ignored when "
                    "cwd is not provided."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._cwd: str | None = None

    def _maybe_rtk(self, command: str) -> str:
        """Wrap *command* with ``rtk`` if the binary is available and enabled."""
        try:
            config = getattr(self, "_agent_config", None)
        except Exception:
            return command
        if config is None:
            return command

        rtk_mode = getattr(config, "shell_rtk", "auto")
        if rtk_mode == "off":
            return command

        has_rtk = shutil.which("rtk") is not None
        if has_rtk:
            return f"rtk {command}"

        if rtk_mode == "on":
            # user wanted rtk but it's missing — emit a soft warning via stderr (one-shot)
            if not getattr(self, "_rtk_warned_missing", False):
                print(
                    "[rtk] shell.rtk=on but rtk not found on PATH, running raw command",
                    file=__import__("sys").stderr,
                )
                self._rtk_warned_missing = True
        return command

    def execute(self, command: str, timeout: int = 120, cwd: str | None = None, persist_cwd: bool = False) -> str:
        return self.run_backend(command=command, timeout=timeout, cwd=cwd, persist_cwd=persist_cwd)

    @backend_handler("remote_relay")
    def _execute_remote(self, command: str, timeout: int = 120, cwd: str | None = None, persist_cwd: bool = False) -> str:
        if not isinstance(command, str) or not command:
            return "Error: shell command must be a non-empty string"
        if not isinstance(timeout, int) or timeout < 1:
            return "Error: timeout must be a positive integer"
        return self.backend.exec_tool("shell", {"command": command, "timeout": timeout, "cwd": cwd, "persist_cwd": persist_cwd})

    @backend_handler("local")
    def _execute_local(self, command: str, timeout: int = 120, cwd: str | None = None, persist_cwd: bool = False) -> str:
        command = self._maybe_rtk(command)

        # Resolve working directory: explicit cwd > persisted session cwd > process cwd
        actual_cwd = cwd or self._cwd or os.getcwd()

        # Validate the directory exists
        if not os.path.isdir(actual_cwd):
            return f"Error: working directory does not exist ({actual_cwd})"

        # Persist cwd for subsequent calls
        if cwd and persist_cwd:
            self._cwd = cwd

        platform_info = get_platform_info()
        shell = platform_info.get_preferred_shell()

        try:
            if platform_info.is_windows and shell in (
                ShellType.POWERSHELL,
                ShellType.POWERSHELL_CORE,
            ):
                proc = self._run_powershell(command, actual_cwd, timeout)
            else:
                # Use explicit shell invocation when available (handles
                # bash on both Windows/Unix, cmd.exe on Windows).
                # Fall back to shell=True only when no shell is detected
                # (e.g. minimal containers without bash/sh).
                shell_cmd = platform_info.get_shell_executable()
                if shell_cmd:
                    proc = self._run_process(
                        shell_cmd + [command], cwd=actual_cwd, timeout=timeout
                    )
                else:
                    proc = self._run_process(
                        command,
                        shell=True,
                        cwd=actual_cwd,
                        timeout=timeout,
                    )

            out = proc.stdout
            if proc.stderr:
                out += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                out += f"\n[exit code: {proc.returncode}]"
            if len(out) > 15_000:
                out = (
                    out[:6000]
                    + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
                    + out[-3000:]
                )
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {timeout}s"
        except ShellCancelled:
            return "Error: shell command cancelled"
        except Exception as e:
            return f"Error running command: {e}"

    def _run_powershell(
        self, command: str, cwd: str, timeout: int
    ) -> subprocess.CompletedProcess:
        """Run a command through PowerShell on Windows.

        PowerShell 5.1 (powershell.exe) does not support ``&&`` and ``||``
        chain operators, so we replace ``&&`` with ``;`` for compatibility.
        PowerShell 7+ (pwsh) supports ``&&`` natively, so we leave it intact.
        """
        platform_info = get_platform_info()
        shell = platform_info.get_preferred_shell()
        shell_cmd = platform_info.get_shell_executable()

        # PowerShell 7+ (pwsh) supports && chain operators natively.
        # Legacy Windows PowerShell 5.1 does not — replace && with ;
        # to avoid cryptic syntax errors from AI-generated commands.
        if shell != ShellType.POWERSHELL_CORE:
            normalized = command.replace("&&", ";")
        else:
            normalized = command

        return self._run_process(
            shell_cmd + [normalized], cwd=cwd, timeout=timeout
        )

    def _run_process(
        self,
        command: str | list[str],
        *,
        cwd: str,
        timeout: int,
        shell: bool = False,
    ) -> subprocess.CompletedProcess:
        cancellation_event = getattr(
            getattr(self.backend, "context", None), "cancellation_event", None
        )
        if cancellation_event is None:
            return subprocess.run(
                command,
                cwd=cwd,
                timeout=timeout,
                shell=shell,
                capture_output=True,
                text=True,
            )
        return self._run_cancellable(
            command, cwd=cwd, timeout=timeout, shell=shell
        )

    def _run_cancellable(
        self,
        command: str | list[str],
        *,
        cwd: str,
        timeout: int,
        shell: bool = False,
    ) -> subprocess.CompletedProcess:
        cancellation_event = self.backend.context.cancellation_event
        popen_kwargs = {
            "cwd": cwd,
            "shell": shell,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **popen_kwargs)
        deadline = time.monotonic() + timeout
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self._terminate_process_tree(process)
                raise ShellCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process_tree(process)
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr
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
