"""Content search with regex support."""

from __future__ import annotations

from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
}


@register_tool
class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents with regex. "
        "Returns matching lines with file path and line number."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: cwd)",
            },
            "include": {
                "type": "string",
                "description": "Only search files matching this glob (e.g. '*.py')",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str | ToolOutcome:
        return self.run_backend(pattern=pattern, path=path, include=include)

    @backend_handler("remote_relay")
    def _execute_remote(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str | ToolOutcome:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern must be a non-empty string"
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        if include is not None and not isinstance(include, str):
            return "Error: include must be a string when provided"
        return self._execute_workspace(pattern, path, include)

    @backend_handler("local")
    def _execute_local(
        self, pattern: str, path: str = ".", include: str | None = None
    ) -> str | ToolOutcome:
        return self._execute_workspace(pattern, path, include)

    def _execute_workspace(
        self, pattern: str, path: str, include: str | None
    ) -> str | ToolOutcome:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern must be a non-empty string"
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        if include is not None and not isinstance(include, str):
            return "Error: include must be a string when provided"
        try:
            result = self.backend.workspace.search_text(
                pattern,
                path,
                include=include,
                exclude_dirs=tuple(sorted(_SKIP_DIRS)),
                max_files=5_000,
                max_matches=200,
            )
            lines = [
                f"{match.path}:{match.line_number}: {match.line}"
                for match in result.matches
            ]
            if result.truncated:
                lines.append("... (200 match limit reached)")
            content = "\n".join(lines) if lines else "No matches found."
            match_count = len(result.matches)
            file_count = len({match.path for match in result.matches})
            return ToolOutcome(
                summary=(
                    f"Found {match_count} match{'es' if match_count != 1 else ''} "
                    f"across {file_count} file{'s' if file_count != 1 else ''}"
                ),
                content=content,
                metadata={
                    "operation": "grep",
                    "pattern": pattern,
                    "path": path,
                    "include": include,
                    "match_count": match_count,
                    "file_count": file_count,
                    "truncated": result.truncated,
                },
            )
        except WorkspaceError as e:
            if e.code.value == "invalid_path" and e.message.startswith(
                "invalid regex:"
            ):
                return "Invalid regex:" + e.message.removeprefix("invalid regex:")
            return f"Error [{e.code.value}]: {e.message}"
        except Exception as e:
            return f"Error: {e}"
