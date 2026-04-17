"""File reading with line numbers."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
class ReadFileTool(Tool):
    name = "read_file"
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
    ) -> str:
        return self.run_backend(
            file_path=file_path,
            offset=offset,
            limit=limit,
            override=override,
        )

    @backend_handler("local")
    def _execute_local(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        override: bool = False,
    ) -> str:
        try:
            p = Path(file_path).expanduser().resolve()
            if not p.exists():
                return f"Error: {file_path} not found"
            if not p.is_file():
                return f"Error: {file_path} is a directory, not a file"

            text = p.read_text(errors="replace")
            lines = text.splitlines()
            total = len(lines)

            if override:
                numbered = [f"{i + 1}\t{ln}" for i, ln in enumerate(lines)]
                return "\n".join(numbered) or "(empty file)"

            start = max(0, offset - 1)
            chunk = lines[start : start + limit]
            numbered = [f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk)]
            result = "\n".join(numbered)

            if total > start + limit:
                result += (
                    f"\n... ({total} lines total, showing {start + 1}-{start + len(chunk)}; "
                    "use override=true to read full file)"
                )
            return result or "(empty file)"
        except Exception as e:
            return f"Error: {e}"
