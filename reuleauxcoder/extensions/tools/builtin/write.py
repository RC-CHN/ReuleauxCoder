"""File creation / overwrite."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.diff import build_tool_diff
from reuleauxcoder.domain.approval_subjects import (
    canonical_workspace_subject,
    file_approval_grant_scopes,
)
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.builtin._workspace_mutation import (
    current_expected_revision,
    project_successful_mutation,
    workspace_mutation_failure,
)
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler


class WriteFileTool(Tool):
    name = "write_file"
    effect_class = "filesystem_mutation"
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

    def approval_grant_scopes(self, arguments, subjects):
        del arguments
        return file_approval_grant_scopes(subjects)

    def execute(  # type: ignore[override]
        self, file_path: str, content: str
    ) -> ToolOutcome:
        return cast(
            ToolOutcome,
            self.run_backend(file_path=file_path, content=content),
        )

    @backend_handler("remote_relay")
    def _execute_remote(self, file_path: str, content: str) -> ToolOutcome:
        if not isinstance(file_path, str) or not file_path:
            return _invalid("Error: file_path must be a non-empty string")
        if not isinstance(content, str):
            return _invalid("Error: content must be a string")
        return cast(ToolOutcome, self._execute_local(file_path, content))

    @backend_handler("local")
    def _execute_local(self, file_path: str, content: str) -> ToolOutcome:
        workspace = self.backend.workspace
        assert workspace is not None
        try:
            result = workspace.write_text_verified(
                file_path,
                content,
                expected_revision=current_expected_revision(self.backend),
            )
            old_content = result.old_content or ""
            n_lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )
            resolved = workspace.resolve(file_path)
            diff = build_tool_diff(old_content, content, str(resolved))
            summary = f"Wrote {n_lines} lines to {file_path}"
            outcome = ToolOutcome(
                summary=summary,
                content=summary,
                diff=diff,
                metadata={
                    "operation": "write",
                    "file_path": file_path,
                    "resolved_path": str(resolved),
                    "line_count": n_lines,
                    "show_diff_by_default": True,
                },
            )
            return project_successful_mutation(
                outcome,
                result.receipt,
                operation="write",
            )
        except WorkspaceError as e:
            return workspace_mutation_failure(
                e,
                operation="write",
                file_path=file_path,
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
