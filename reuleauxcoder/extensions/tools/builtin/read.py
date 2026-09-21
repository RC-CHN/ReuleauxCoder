"""File reading with line numbers."""

from __future__ import annotations

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.workspace import WorkspaceError, WorkspaceErrorCode
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler


class ReadFileTool(Tool):
    name = "read_file"
    parallel_safe = True
    description = (
        "Read a file's contents with line numbers. "
        "Always read a file before editing it. "
        "For large files, prefer paged reads with offset/limit; use override=true only when you intentionally need the full file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file",
            },
            "offset": {
                "type": "integer",
                "description": "Start line (1-based). Default 1.",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to read. Default 2000.",
            },
            "override": {
                "type": "boolean",
                "description": "If true, ignore offset/limit and read the full file. Default false.",
            },
        },
        "required": ["file_path"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        override: bool = False,
    ) -> ToolOutcome:
        return self.run_backend(
            file_path=file_path,
            offset=offset,
            limit=limit,
            override=override,
        )

    @backend_handler("remote_relay")
    def _execute_remote(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        override: bool = False,
    ) -> ToolOutcome:
        if not isinstance(file_path, str) or not file_path:
            return _invalid("Error: file_path must be a non-empty string")
        if not isinstance(offset, int) or offset < 1:
            return _invalid("Error: offset must be a positive integer")
        if not isinstance(limit, int) or limit < 1:
            return _invalid("Error: limit must be a positive integer")
        if not isinstance(override, bool):
            return _invalid("Error: override must be a boolean")
        return self._execute_local(file_path, offset, limit, override)

    @backend_handler("local")
    def _execute_local(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        override: bool = False,
    ) -> ToolOutcome:
        try:
            if override:
                text = self.backend.workspace.read_text(file_path)
                lines = text.splitlines()
                total = len(lines)
                start = 0
                chunk = lines
                has_more = truncated = False
            else:
                page = self.backend.workspace.read_text_page(
                    file_path, offset=offset, limit=limit
                )
                start = offset - 1
                chunk = page.lines
                total = page.total_lines
                has_more = page.has_more
                truncated = page.truncated
            numbered = [f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk)]
            result = "\n".join(numbered)

            if truncated:
                result += "\n... (page character limit reached; use grep for targeted text or override=true for the full file)"
            elif has_more:
                result += f"\n... (more lines available; continue with offset={start + len(chunk) + 1})"
            model_content = result or "(empty file)"
            source_chars = sum(map(len, chunk)) + max(0, len(chunk) - 1)
            if chunk:
                line_range = f"lines {start + 1}-{start + len(chunk)}"
                if total is not None:
                    line_range += f" of {total}"
            else:
                line_range = f"0 of {total} lines"
            return ToolOutcome(
                summary=(f"Read {line_range} ({source_chars} chars) from {file_path}"),
                content=model_content,
                metadata={
                    "operation": "read",
                    "file_path": file_path,
                    "offset": start + 1,
                    "line_count": len(chunk),
                    "total_lines": total,
                    "character_count": source_chars,
                    "override": override,
                    "has_more": has_more,
                    "truncated": truncated,
                },
                retention_hint=ToolRetentionHint(
                    strategy=ToolRetentionStrategy.HEAD,
                    anchor_line=start + 1,
                ),
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
