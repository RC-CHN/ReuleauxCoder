"""Content search with regex support."""

from __future__ import annotations

from typing import ClassVar

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import InterruptMode, Tool, backend_handler

_SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "dist",
    "build",
    ".rcoder",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class GrepTool(Tool):
    name = "grep"
    parallel_safe = True
    interrupt_mode = InterruptMode.CANCEL_WITH_PARTIAL
    description = (
        "Search text files; returns paths, line numbers and matching lines. "
        "Local regex uses Python-compatible syntax; remote regex uses Go/RE2 "
        "(no lookaround or backreferences; \\w/\\d/\\s are ASCII). "
        "Uses Git ignore rules in repositories when Git is available. "
        "Binary files are skipped; size, time and output limits report partial results. "
        "Narrow path/include when results are incomplete."
    )
    parameters: ClassVar[dict] = {
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
                "description": "Case-sensitive glob relative to path; '*.py' matches at any depth; use 'src/**/*.py' for a subtree. Remote supports *, ? and **, not character classes.",
            },
            "literal": {
                "type": "boolean",
                "description": "Match plain text instead of regex (default false)",
            },
            "include_ignored": {
                "type": "boolean",
                "description": "Include Git-ignored files (default false); dependency/cache directories remain excluded",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self,
        pattern: str,
        path: str = ".",
        include: str | None = None,
        literal: bool = False,
        include_ignored: bool = False,
    ) -> str | ToolOutcome:
        return self.run_backend(
            pattern=pattern,
            path=path,
            include=include,
            literal=literal,
            include_ignored=include_ignored,
        )

    @backend_handler("local")
    def _execute_workspace(
        self,
        pattern: str,
        path: str,
        include: str | None,
        literal: bool,
        include_ignored: bool,
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
                literal=literal,
                include_ignored=include_ignored,
                cancellation=self.backend.current_cancellation_signal(),
            )
            lines = [
                f"{match.path}:{match.line_number}: {match.line}"
                + (" … [line truncated]" if match.truncated else "")
                for match in result.matches
            ]
            if result.truncated:
                lines.append(
                    "... (partial search: "
                    + ", ".join(result.reasons)
                    + "; narrow path/include or read the matching file)"
                )
            content = "\n".join(lines) if lines else "No matches found."
            match_count = len(result.matches)
            file_count = len({match.path for match in result.matches})
            status = ToolOutcomeStatus.SUCCEEDED
            if "cancelled" in result.reasons:
                status = ToolOutcomeStatus.CANCELLED
            elif {"timeout", "regex_timeout"}.intersection(result.reasons):
                status = ToolOutcomeStatus.TIMED_OUT
            return ToolOutcome(
                status=status,
                summary=(
                    f"Found {match_count} match{'es' if match_count != 1 else ''} "
                    f"across {file_count} file{'s' if file_count != 1 else ''}"
                )
                + (" (partial)" if result.truncated else ""),
                content=content,
                metadata={
                    "operation": "grep",
                    "pattern": pattern,
                    "path": path,
                    "include": include,
                    "match_count": match_count,
                    "file_count": file_count,
                    "truncated": result.truncated,
                    "reasons": list(result.reasons),
                    "scanned_files": result.scanned_files,
                    "scanned_bytes": result.scanned_bytes,
                },
                retention_hint=ToolRetentionHint(
                    strategy=ToolRetentionStrategy.HEAD_TAIL
                ),
            )
        except WorkspaceError as e:
            if e.code.value == "invalid_path" and e.message.startswith(
                "invalid regex:"
            ):
                return "Invalid regex:" + e.message.removeprefix("invalid regex:")
            return f"Error [{e.code.value}]: {e.message}"
