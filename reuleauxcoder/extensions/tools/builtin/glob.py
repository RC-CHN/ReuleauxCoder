"""File pattern matching."""

from __future__ import annotations

from pathlib import PurePath

from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files matching a glob pattern. "
        "Supports ** for recursive matching (e.g. '**/*.py')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: cwd)",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(self, pattern: str, path: str = ".") -> str:
        return self.run_backend(pattern=pattern, path=path)

    @backend_handler("remote_relay")
    def _execute_remote(self, pattern: str, path: str = ".") -> str:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern must be a non-empty string"
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        return self._execute_workspace(pattern, path)

    @backend_handler("local")
    def _execute_local(self, pattern: str, path: str = ".") -> str:
        return self._execute_workspace(pattern, path)

    def _execute_workspace(self, pattern: str, path: str) -> str:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern must be a non-empty string"
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        try:
            base = self.backend.workspace.stat_entry(path)
            if not base.is_dir:
                return f"Error: {path} is not a directory"
            listing = self.backend.workspace.list_entries(
                path,
                recursive=True,
                include_hidden=True,
                max_entries=20_000,
            )
            hits = [
                entry
                for entry in listing.entries
                if PurePath(entry.relative_path).full_match(pattern)
            ]
            hits.sort(key=lambda entry: entry.mtime, reverse=True)
            total = len(hits)
            shown = hits[:100]
            lines = [entry.path for entry in shown]
            result = "\n".join(lines)

            if total > 100:
                result += f"\n... ({total} matches, showing first 100)"
            elif listing.truncated:
                result += "\n... (workspace listing limit reached)"
            return result or "No files matched."
        except WorkspaceError as e:
            return f"Error [{e.code.value}]: {e.message}"
        except Exception as e:
            return f"Error: {e}"
