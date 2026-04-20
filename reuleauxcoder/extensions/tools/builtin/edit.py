"""Search-and-replace file editing."""

from __future__ import annotations

import difflib
from pathlib import Path

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
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

    def preflight_validate(self, file_path: str, old_string: str, new_string: str) -> str | None:
        """Fast validation so invalid edit requests can be rejected before approval."""
        if getattr(self.backend, "backend_id", "local") == "remote_relay":
            if not isinstance(file_path, str) or not file_path:
                return "Error: edit_file requires a valid string file_path"
            if not isinstance(old_string, str) or not isinstance(new_string, str):
                return "Error: edit_file requires string old_string and new_string"
            if old_string == new_string:
                return "Error: old_string and new_string must differ"
            return None
        return _validate_edit_request(file_path, old_string, new_string)

    def execute(self, file_path: str, old_string: str, new_string: str) -> str:
        validation_error = self.preflight_validate(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
        )
        if validation_error:
            return validation_error
        return self.run_backend(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
        )

    @backend_handler("remote_relay")
    def _execute_remote(self, file_path: str, old_string: str, new_string: str) -> str:
        validation_error = self.preflight_validate(
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
        )
        if validation_error:
            return validation_error
        return self.backend.exec_tool(
            "edit_file",
            {"file_path": file_path, "old_string": old_string, "new_string": new_string},
        )

    @backend_handler("local")
    def _execute_local(self, file_path: str, old_string: str, new_string: str) -> str:
        try:
            p = Path(file_path).expanduser().resolve()
            content = p.read_text()
            new_content = content.replace(old_string, new_string, 1)
            p.write_text(new_content)

            diff = _unified_diff(content, new_content, str(p))
            return f"Edited {file_path}\n{diff}"
        except Exception as e:
            return f"Error: {e}"


def _validate_edit_request(file_path: str, old_string: str, new_string: str) -> str | None:
    if not isinstance(file_path, str) or not file_path:
        return "Error: edit_file requires a valid string file_path"
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Error: edit_file requires string old_string and new_string"
    if old_string == new_string:
        return "Error: old_string and new_string must differ"

    p = Path(file_path).expanduser().resolve()
    if not p.exists():
        return f"Error: {file_path} not found"

    content = p.read_text()
    occurrences = content.count(old_string)
    if occurrences == 0:
        return (
            f"Error: old_string not found in {file_path}. "
            "Include exact text with enough surrounding context."
        )
    if occurrences > 1:
        return (
            f"Error: old_string appears {occurrences} times in {file_path}. "
            "Include more surrounding lines to make it unique."
        )
    return None


def _unified_diff(old: str, new: str, filename: str, context: int = 3) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=context,
    )
    result = "".join(diff)
    if len(result) > 3000:
        result = result[:2500] + "\n... (diff truncated)\n"
    return result
