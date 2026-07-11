"""File creation / overwrite."""

from __future__ import annotations

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.tools.builtin._diff import build_tool_diff
from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a new file or completely overwrite an existing one. "
        "For small edits to existing files, prefer edit_file instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path for the file",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write",
            },
        },
        "required": ["file_path", "content"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(self, file_path: str, content: str) -> ToolOutcome:
        return self.run_backend(file_path=file_path, content=content)

    @backend_handler("remote_relay")
    def _execute_remote(self, file_path: str, content: str) -> ToolOutcome:
        if not isinstance(file_path, str) or not file_path:
            return _invalid("Error: file_path must be a non-empty string")
        if not isinstance(content, str):
            return _invalid("Error: content must be a string")
        return self._execute_local(file_path, content)

    @backend_handler("local")
    def _execute_local(self, file_path: str, content: str) -> ToolOutcome:
        try:
            old_content = self.backend.workspace.write_text_atomic(file_path, content)
            n_lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )
            resolved = self.backend.workspace.resolve(file_path)
            diff = build_tool_diff(old_content, content, str(resolved))
            summary = f"Wrote {n_lines} lines to {file_path}"
            return ToolOutcome(
                summary=summary,
                content=summary,
                diff=diff,
                metadata={
                    "file_path": file_path,
                    "resolved_path": str(resolved),
                    "line_count": n_lines,
                },
            )
        except WorkspaceError as e:
            kind = (
                ToolErrorKind.NOT_FOUND
                if e.code is WorkspaceErrorCode.NOT_FOUND
                else ToolErrorKind.EXECUTION
            )
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=f"Error [{e.code.value}]: {e.message}",
                error_kind=kind,
                metadata={"workspace_error_code": e.code.value},
            )
        except Exception as e:
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=f"Error: {e}",
                error_kind=ToolErrorKind.EXECUTION,
            )


def _invalid(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=message,
        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
    )
