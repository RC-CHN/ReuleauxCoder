"""Search-and-replace file editing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.diff import build_tool_diff
from reuleauxcoder.domain.approval_subjects import canonical_workspace_subject
from reuleauxcoder.domain.workspace import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspacePort,
)
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Edit a file by replacing an exact string match. "
        "old_string must appear exactly once in the file for safety. "
        "Include enough surrounding context to ensure uniqueness."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to edit",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to find (must be unique in file)",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def approval_subjects(
        self, arguments: Mapping[str, Any]
    ) -> tuple[str, ...]:
        file_path = arguments.get("file_path")
        if not isinstance(file_path, str):
            return ()
        subject = canonical_workspace_subject(
            self.backend.workspace,
            file_path,
        )
        return (subject,) if subject is not None else ()

    def _preflight_validate(  # type: ignore[override]
        self, file_path: str, old_string: str, new_string: str
    ) -> str | None:
        """Fast validation so invalid edit requests can be rejected before approval."""
        return _validate_edit_request(
            file_path, old_string, new_string, workspace=self.backend.workspace
        )

    def execute(  # type: ignore[override]
        self, file_path: str, old_string: str, new_string: str
    ) -> ToolOutcome:
        validation_error = self.preflight_validate(
            {
                "file_path": file_path,
                "old_string": old_string,
                "new_string": new_string,
            }
        )
        if validation_error:
            return validation_error
        return cast(
            ToolOutcome,
            self.run_backend(
                file_path=file_path,
                old_string=old_string,
                new_string=new_string,
            ),
        )

    @backend_handler("remote_relay")
    def _execute_remote(
        self, file_path: str, old_string: str, new_string: str
    ) -> ToolOutcome:
        return cast(
            ToolOutcome,
            self._execute_local(file_path, old_string, new_string),
        )

    @backend_handler("local")
    def _execute_local(
        self, file_path: str, old_string: str, new_string: str
    ) -> ToolOutcome:
        workspace = self.backend.workspace
        assert workspace is not None
        try:
            content, new_content = workspace.replace_exact_atomic(
                file_path, old_string, new_string
            )
            resolved = workspace.resolve(file_path)
            diff = build_tool_diff(content, new_content, str(resolved))
            summary = f"Edited {file_path}"
            return ToolOutcome(
                summary=summary,
                content=summary,
                diff=diff,
                metadata={
                    "operation": "edit",
                    "file_path": file_path,
                    "resolved_path": str(resolved),
                    "show_diff_by_default": True,
                },
            )
        except WorkspaceError as e:
            return _workspace_failure(e)
        except Exception as e:
            return ToolOutcome(
                status=ToolOutcomeStatus.FAILED,
                content=f"Error: {e}",
                error_kind=ToolErrorKind.EXECUTION,
            )


def _validate_edit_request(
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    workspace: WorkspacePort | None = None,
) -> str | None:
    if not isinstance(file_path, str) or not file_path:
        return "Error: edit_file requires a valid string file_path"
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Error: edit_file requires string old_string and new_string"
    if old_string == new_string:
        return "Error: old_string and new_string must differ"

    if workspace is None:
        workspace = LocalToolBackend().workspace
    assert workspace is not None
    try:
        content = workspace.read_text(file_path)
    except WorkspaceError as error:
        return f"Error [{error.code.value}]: {error.message}"
    occurrences = content.count(old_string)
    if occurrences == 0:
        return (
            f"Error [{WorkspaceErrorCode.NOT_FOUND.value}]: old_string not found in {file_path}. "
            "Include exact text with enough surrounding context."
        )
    if occurrences > 1:
        return (
            f"Error [{WorkspaceErrorCode.NOT_UNIQUE.value}]: old_string appears {occurrences} times in {file_path}. "
            "Include more surrounding lines to make it unique."
        )
    return None


def _workspace_failure(error: WorkspaceError) -> ToolOutcome:
    kind = (
        ToolErrorKind.NOT_FOUND
        if error.code is WorkspaceErrorCode.NOT_FOUND
        else ToolErrorKind.EXECUTION
    )
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=f"Error [{error.code.value}]: {error.message}",
        error_kind=kind,
        metadata={"workspace_error_code": error.code.value},
    )
