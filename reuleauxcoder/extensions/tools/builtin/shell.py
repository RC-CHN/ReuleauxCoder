"""Shell command execution with safety checks."""

from __future__ import annotations

import os
import re
import subprocess

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.infrastructure.platform import ShellType, get_platform_info


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
        },
        "required": ["command"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._cwd: str | None = None

    def execute(self, command: str, timeout: int = 120) -> str:
        return self.run_backend(command=command, timeout=timeout)

    @backend_handler("remote_relay")
    def _execute_remote(self, command: str, timeout: int = 120) -> str:
        if not isinstance(command, str) or not command:
            return "Error: shell command must be a non-empty string"
        if not isinstance(timeout, int) or timeout < 1:
            return "Error: timeout must be a positive integer"
        return self.backend.exec_tool("shell", {"command": command, "timeout": timeout})

    @backend_handler("local")
    def _execute_local(self, command: str, timeout: int = 120) -> str:
        cwd = self._cwd or os.getcwd()

        # Detect stale CWD (e.g. deleted temp dir) and reset to workspace root
        if self._cwd is not None and not os.path.isdir(self._cwd):
            self._cwd = None
            return (
                f"Error: working directory no longer exists ({cwd}). "
                "Directory has been reset to the project root."
            )

        platform_info = get_platform_info()
        shell = platform_info.get_preferred_shell()

        try:
            if platform_info.is_windows and shell in (
                ShellType.POWERSHELL,
                ShellType.POWERSHELL_CORE,
            ):
                proc = self._run_powershell(command, cwd, timeout)
            elif platform_info.is_windows and shell == ShellType.BASH:
                # On Windows, shell=True always invokes cmd.exe regardless
                # of $SHELL. When Git Bash is the preferred shell, run it
                # explicitly so Unix commands (grep, sed, &&, etc.) work.
                shell_path = platform_info.get_shell_path()
                proc = subprocess.run(
                    [shell_path, "-c", command],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                )
            else:
                proc = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                )

            if proc.returncode == 0:
                self._update_cwd(command, cwd, platform_info.is_windows)

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
        except Exception as e:
            return f"Error running command: {e}"

    def _run_powershell(
        self, command: str, cwd: str, timeout: int
    ) -> subprocess.CompletedProcess:
        """Run a command through PowerShell on Windows."""
        platform_info = get_platform_info()
        shell_cmd = platform_info.get_shell_executable()

        normalized = command.replace("&&", ";")

        proc = subprocess.run(
            shell_cmd + [normalized],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc

    def _update_cwd(
        self, command: str, current_cwd: str, is_windows: bool = False
    ) -> None:
        if is_windows:
            parts = re.split(r"[;]|\n", command)
        else:
            parts = re.split(r"&&|;|\n", command)

        for part in parts:
            part = part.strip()
            if not part:
                continue

            target: str | None = None
            if part.startswith("cd "):
                target = part[3:].strip().strip("'\"")
            elif part.lower().startswith("set-location "):
                target = part[13:].strip().strip("'\"")
            elif part.lower().startswith("chdir "):
                target = part[6:].strip().strip("'\"")
            elif len(part) > 3 and part.lower().startswith("sl "):
                target = part[3:].strip().strip("'\"")

            if target:
                new_dir = os.path.normpath(
                    os.path.join(current_cwd, os.path.expanduser(target))
                )
                if os.path.isdir(new_dir):
                    self._cwd = new_dir
