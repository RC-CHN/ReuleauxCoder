"""Shell command execution with safety checks."""

import os
import re
import subprocess

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.infrastructure.platform import get_platform_info, ShellType

_cwd: str | None = None


class BashTool(Tool):
    name = "bash"
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

    def execute(self, command: str, timeout: int = 120) -> str:
        global _cwd
        cwd = _cwd or os.getcwd()
        platform_info = get_platform_info()
        shell = platform_info.get_preferred_shell()

        try:
            if platform_info.is_windows and shell in (
                ShellType.POWERSHELL,
                ShellType.POWERSHELL_CORE,
            ):
                proc = self._run_powershell(command, cwd, timeout)
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
                _update_cwd(command, cwd, platform_info.is_windows)

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

        # PowerShell 5.x doesn't support &&, so normalize to ;
        normalized = command.replace("&&", ";")

        proc = subprocess.run(
            shell_cmd + [normalized],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc


def _update_cwd(command: str, current_cwd: str, is_windows: bool = False) -> None:
    global _cwd
    # Split by common command separators
    if is_windows:
        parts = re.split(r"[;]|\n", command)
    else:
        parts = re.split(r"&&|;|\n", command)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        target: str | None = None

        # Unix bash style: cd <dir>
        if part.startswith("cd "):
            target = part[3:].strip().strip("'\"")
        # PowerShell style: cd <dir>, Set-Location <dir>, sl <dir>
        elif part.lower().startswith("set-location "):
            target = part[13:].strip().strip("'\"")
        elif part.lower().startswith("chdir "):
            target = part[6:].strip().strip("'\"")
        elif len(part) > 3 and part.lower().startswith("cd "):
            target = part[3:].strip().strip("'\"")
        elif len(part) > 3 and part.lower().startswith("sl "):
            target = part[3:].strip().strip("'\"")

        if target:
            new_dir = os.path.normpath(
                os.path.join(current_cwd, os.path.expanduser(target))
            )
            if os.path.isdir(new_dir):
                _cwd = new_dir
