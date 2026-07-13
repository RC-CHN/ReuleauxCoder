"""Tools for durable workspace and global notes."""

from __future__ import annotations

from pathlib import Path

from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.infrastructure.persistence.notes_store import NoteStore


class _NoteTool(Tool):
    effect_class = "control_plane_internal"

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None

    def bind_agent(self, agent) -> None:
        self._agent = agent

    def _store(self) -> NoteStore:
        bound = getattr(self._agent, "notes_store", None)
        if isinstance(bound, NoteStore):
            return bound
        context = getattr(self.backend, "context", None)
        root = getattr(context, "workspace_root", None) or Path.cwd()
        config = getattr(self, "_agent_config", None)
        return NoteStore(
            Path(root),
            workspace_max=getattr(config, "notes_workspace_max", 30),
            global_max=getattr(config, "notes_global_max", 20),
        )


@register_tool
class WriteNoteTool(_NoteTool):
    name = "write_note"
    description = (
        "Create a concise durable note. workspace notes belong only to the "
        "current project; global notes are user preferences shared by every "
        "project. Notes appear as untrusted data in the final execution_state "
        "overlay. The result contains the stable ID needed to edit or delete it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "Concise note text",
            },
            "scope": {
                "type": "string",
                "enum": ["workspace", "global"],
                "description": (
                    "workspace = this project only; global = all projects "
                    "(default: workspace)"
                ),
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    def execute(self, content: str, scope: str = "workspace") -> str:
        entry = self._store().write(content, scope=scope)
        return f"Created {scope} note {entry.id}."


@register_tool
class EditNoteTool(_NoteTool):
    name = "edit_note"
    description = (
        "Replace the content of one durable note without deleting and recreating "
        "it. The stable note ID and its explicit workspace/global scope must match."
    )
    parameters = {
        "type": "object",
        "properties": {
            "note_id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable ID shown in the execution_state notes list",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "description": "Complete replacement note text",
            },
            "scope": {
                "type": "string",
                "enum": ["workspace", "global"],
                "description": "The note's workspace/global scope",
            },
        },
        "required": ["note_id", "content", "scope"],
        "additionalProperties": False,
    }

    def execute(self, note_id: str, content: str, scope: str) -> str:
        entry = self._store().edit(note_id, content, scope=scope)
        if entry is None:
            return f"No {scope} note with ID {note_id}."
        return f"Updated {scope} note {entry.id}."


@register_tool
class DeleteNoteTool(_NoteTool):
    name = "delete_note"
    description = (
        "Delete one durable note by stable ID. The explicit workspace/global "
        "scope must match the note."
    )
    parameters = {
        "type": "object",
        "properties": {
            "note_id": {
                "type": "string",
                "minLength": 1,
                "description": "Stable ID shown in the execution_state notes list",
            },
            "scope": {
                "type": "string",
                "enum": ["workspace", "global"],
                "description": "The note's workspace/global scope",
            },
        },
        "required": ["note_id", "scope"],
        "additionalProperties": False,
    }

    def execute(
        self,
        scope: str,
        note_id: str,
    ) -> str:
        entry = self._store().delete(scope=scope, note_id=note_id)
        if entry is None:
            return f"No {scope} note with ID {note_id}."
        return f"Deleted {scope} note {entry.id}."
