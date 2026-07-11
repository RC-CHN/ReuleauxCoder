"""Shell product tool implemented over a platform-neutral ProcessPort."""

from __future__ import annotations

import os
import shutil
import time

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
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
    ) -> ToolOutcome:
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
    ) -> ToolOutcome:
        if not isinstance(command, str) or not command:
            return _invalid("Error: shell command must be a non-empty string")
        if not isinstance(timeout, int) or timeout < 1:
            return _invalid("Error: timeout must be a positive integer")
        supports = getattr(self.backend, "supports_capability", None)
        if callable(supports) and not supports("process.start"):
            return self.backend.exec_tool_outcome(
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
    ) -> ToolOutcome:
        return self._execute_process(command, timeout, cwd, persist_cwd)

    def _execute_process(
        self,
        command: str,
        timeout: int,
        cwd: str | None,
        persist_cwd: bool,
    ) -> ToolOutcome:
        if not isinstance(command, str) or not command:
            return _invalid("Error: shell command must be a non-empty string")
        if not isinstance(timeout, int) or timeout < 1:
            return _invalid("Error: timeout must be a positive integer")
        if cwd is not None and not isinstance(cwd, str):
            return _invalid("Error: cwd must be a string when provided")
        if not isinstance(persist_cwd, bool):
            return _invalid("Error: persist_cwd must be a boolean")
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
        started = time.monotonic()
        try:
            result = self.backend.process.run(
                command,
                cwd=actual_cwd,
                timeout=timeout,
                cancellation_event=context.cancellation_event,
                stream_handler=self._stream_handler(),
            )
        except FileNotFoundError:
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=f"Error: working directory does not exist ({actual_cwd})",
                duration_seconds=time.monotonic() - started,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        except Exception as error:
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=f"Error running command: {error}",
                duration_seconds=time.monotonic() - started,
                error_kind=ToolErrorKind.EXECUTION,
            )
        duration = time.monotonic() - started
        if result.timed_out:
            return ToolOutcome(
                status=ToolOutcomeStatus.TIMED_OUT,
                content=f"Error: timed out after {timeout}s",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                duration_seconds=duration,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        if result.cancelled:
            return ToolOutcome(
                status=ToolOutcomeStatus.CANCELLED,
                content="Error: shell command cancelled",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                duration_seconds=duration,
                error_kind=ToolErrorKind.INTERRUPTED,
            )
        failed = result.exit_code not in {None, 0}
        first_line = next(
            (
                line.strip()
                for line in (result.stdout or result.stderr).splitlines()
                if line.strip()
            ),
            "(no output)",
        )
        if len(first_line) > 120:
            first_line = first_line[:117] + "..."
        return ToolOutcome(
            status=(ToolOutcomeStatus.FAILED if failed else ToolOutcomeStatus.SUCCEEDED),
            summary=(
                f"Command failed (exit {result.exit_code}) · {first_line}"
                if failed
                else f"Command completed · {first_line}"
            ),
            content=(
                "(no output)" if not result.stdout and not result.stderr else None
            ),
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            exit_code=result.exit_code,
            duration_seconds=duration,
            error_kind=ToolErrorKind.EXECUTION if failed else None,
            metadata={"cwd": str(actual_cwd)},
        )

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


def _invalid(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=message,
        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
    )
