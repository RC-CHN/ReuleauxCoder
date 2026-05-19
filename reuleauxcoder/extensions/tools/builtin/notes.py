"""Write a note to the agent's long-term memory (FIFO)."""

from __future__ import annotations

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.infrastructure.persistence.notes_store import write_note


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
