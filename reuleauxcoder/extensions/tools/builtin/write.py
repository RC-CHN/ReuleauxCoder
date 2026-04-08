"""File creation / overwrite."""

import difflib
from pathlib import Path

from reuleauxcoder.extensions.tools.base import Tool


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

    def execute(self, file_path: str, content: str) -> str:
        try:
            p = Path(file_path).expanduser().resolve()
            old_content = p.read_text() if p.exists() else ""
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            n_lines = content.count("\n") + (
                1 if content and not content.endswith("\n") else 0
            )
            diff = _unified_diff(old_content, content, str(p))
            return f"Wrote {n_lines} lines to {file_path}\n{diff}"
        except Exception as e:
            return f"Error: {e}"
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
