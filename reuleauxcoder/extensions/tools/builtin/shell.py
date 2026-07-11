"""Shell product tool implemented over a platform-neutral ProcessPort."""

from __future__ import annotations

import os
import shutil

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


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
                    "Working directory for this command. Defaults to the "
                    "session's current working directory."
                ),
            },
            "persist_cwd": {
                "type": "boolean",
                "description": (
                    "Persist the provided cwd as this session's default."
                ),
            },
        },
        "required": ["command"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._cwd: str | None = None

    def _maybe_rtk(self, command: str) -> str:
        config = getattr(self, "_agent_config", None)
        if config is None:
            return command
        rtk_mode = getattr(config, "shell_rtk", "auto")
        if rtk_mode == "off":
            return command
        if shutil.which("rtk") is not None:
            return f"rtk {command}"
        if rtk_mode == "on" and not getattr(self, "_rtk_warned_missing", False):
            print(
                "[rtk] shell.rtk=on but rtk not found on PATH, running raw command",
                file=__import__("sys").stderr,
            )
            self._rtk_warned_missing = True
        return command

    def execute(
        self,
        command: str,
        timeout: int = 120,
        cwd: str | None = None,
        persist_cwd: bool = False,
    ) -> str:
        return self.run_backend(
            command=command,
            timeout=timeout,
            cwd=cwd,
            persist_cwd=persist_cwd,
        )

    @backend_handler("remote_relay")
    def _execute_remote(
        self,
        command: str,
        timeout: int = 120,
        cwd: str | None = None,
        persist_cwd: bool = False,
    ) -> str:
        if not isinstance(command, str) or not command:
            return "Error: shell command must be a non-empty string"
        if not isinstance(timeout, int) or timeout < 1:
            return "Error: timeout must be a positive integer"
        supports = getattr(self.backend, "supports_capability", None)
        if callable(supports) and not supports("process.start"):
            return self.backend.exec_tool(
                "shell",
                {
                    "command": command,
                    "timeout": timeout,
                    "cwd": cwd,
                    "persist_cwd": persist_cwd,
                },
            )
        return self._execute_process(command, timeout, cwd, persist_cwd)

    @backend_handler("local")
    def _execute_local(
        self,
        command: str,
        timeout: int = 120,
        cwd: str | None = None,
        persist_cwd: bool = False,
    ) -> str:
        return self._execute_process(command, timeout, cwd, persist_cwd)

    def _execute_process(
        self,
        command: str,
        timeout: int,
        cwd: str | None,
        persist_cwd: bool,
    ) -> str:
        if not isinstance(command, str) or not command:
            return "Error: shell command must be a non-empty string"
        if not isinstance(timeout, int) or timeout < 1:
            return "Error: timeout must be a positive integer"
        if cwd is not None and not isinstance(cwd, str):
            return "Error: cwd must be a string when provided"
        if not isinstance(persist_cwd, bool):
            return "Error: persist_cwd must be a boolean"
        command = self._maybe_rtk(command)
        context = self.backend.context
        actual_cwd = (
            cwd
            or self._cwd
            or context.cwd
            or context.workspace_root
            or ("." if self.backend.backend_id == "remote_relay" else os.getcwd())
        )
        if cwd and persist_cwd:
            self._cwd = cwd
        try:
            result = self.backend.process.run(
                command,
                cwd=actual_cwd,
                timeout=timeout,
                cancellation_event=context.cancellation_event,
                stream_handler=self._stream_handler(),
            )
        except FileNotFoundError:
            return f"Error: working directory does not exist ({actual_cwd})"
        except Exception as error:
            return f"Error running command: {error}"
        if result.timed_out:
            return f"Error: timed out after {timeout}s"
        if result.cancelled:
            return "Error: shell command cancelled"
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        if result.exit_code not in {None, 0}:
            output += f"\n[exit code: {result.exit_code}]"
        if len(output) > 15_000:
            output = (
                output[:6000]
                + f"\n\n... truncated ({len(output)} chars total) ...\n\n"
                + output[-3000:]
            )
        return output.strip() or "(no output)"

    def _stream_handler(self):
        build = getattr(self.backend, "_build_stream_handler", None)
        if not callable(build):
            return None
        remote_handler = build("shell")
        if remote_handler is None:
            return None

        def handle(chunk) -> None:
            from reuleauxcoder.extensions.remote_exec.protocol import ToolStreamChunk

            remote_handler(
                ToolStreamChunk(chunk_type=chunk.stream, data=chunk.data)
            )

        return handle
