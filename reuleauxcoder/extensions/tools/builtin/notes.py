"""Write a note to the agent's long-term memory (FIFO)."""

from __future__ import annotations

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.infrastructure.persistence.notes_store import (
    delete_note,
    write_note,
)


@register_tool
class WriteNoteTool(Tool):
    name = "write_note"
    description = (
        "Write a note to long-term memory.  Notes are injected into your "
        "context each turn (in <system_context>) and persist across sessions.  "
        "Use for user preferences, project conventions, or anything you want "
        "to remember later.  Oldest notes are automatically discarded when the "
        "FIFO queue is full."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The note text (concise, one or two sentences)",
            },
            "scope": {
                "type": "string",
                "enum": ["workspace", "global"],
                "description": (
                    "workspace = project-specific, global = cross-project "
                    "(default: workspace)"
                ),
            },
        },
        "required": ["content"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(self, content: str, scope: str = "workspace") -> str:
        write_note(content, scope=scope)
        return f"Noted ({scope})."


@register_tool
class DeleteNoteTool(Tool):
    name = "delete_note"
    description = (
        "Delete a note from long-term memory by its index (1-based, as shown "
        "in the <system_context> workspace/global notes list).  Returns a "
        "confirmation or error if the index is out of range."
    )
    parameters = {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "The 1-based index of the note to delete",
            },
            "scope": {
                "type": "string",
                "enum": ["workspace", "global"],
                "description": (
                    "workspace = project-specific, global = cross-project "
                    "(default: workspace)"
                ),
            },
        },
        "required": ["index"],
    }

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(self, index: int, scope: str = "workspace") -> str:
        ok = delete_note(index, scope=scope)
        if ok:
            return f"Deleted note [{index}] ({scope})."
        return f"No note at index {index} ({scope})."
