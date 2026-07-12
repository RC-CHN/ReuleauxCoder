"""File pattern matching."""

from __future__ import annotations

from functools import lru_cache
from fnmatch import fnmatchcase

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


def _glob_full_match(relative_path: str, pattern: str) -> bool:
    """Match path segments with portable ``**`` semantics on Python 3.10+."""
    path_parts = tuple(
        part for part in relative_path.replace("\\", "/").split("/") if part
    )
    pattern_parts = tuple(
        part for part in pattern.replace("\\", "/").split("/") if part
    )
    if not path_parts or not pattern_parts:
        return False

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        segment = pattern_parts[pattern_index]
        if segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], segment)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


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

    def execute(self, pattern: str, path: str = ".") -> str | ToolOutcome:
        return self.run_backend(pattern=pattern, path=path)

    @backend_handler("remote_relay")
    def _execute_remote(self, pattern: str, path: str = ".") -> str | ToolOutcome:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern must be a non-empty string"
        if not isinstance(path, str) or not path:
            return "Error: path must be a non-empty string"
        return self._execute_workspace(pattern, path)

    @backend_handler("local")
    def _execute_local(self, pattern: str, path: str = ".") -> str | ToolOutcome:
        return self._execute_workspace(pattern, path)

    def _execute_workspace(self, pattern: str, path: str) -> str | ToolOutcome:
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
                if _glob_full_match(entry.relative_path, pattern)
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
            content = result or "No files matched."
            return ToolOutcome(
                summary=f"Found {total} file{'s' if total != 1 else ''} matching {pattern}",
                content=content,
                metadata={
                    "operation": "glob",
                    "pattern": pattern,
                    "path": path,
                    "match_count": total,
                    "shown_count": len(shown),
                    "truncated": total > len(shown) or listing.truncated,
                },
                retention_hint=ToolRetentionHint(
                    strategy=ToolRetentionStrategy.HEAD_TAIL
                ),
            )
        except WorkspaceError as e:
            return f"Error [{e.code.value}]: {e.message}"
        except Exception as e:
            return f"Error: {e}"
