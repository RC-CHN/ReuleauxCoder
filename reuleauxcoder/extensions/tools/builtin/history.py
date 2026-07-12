"""Read-only access to append-only session truth and archived artifacts."""

from __future__ import annotations

import json
from pathlib import Path
import re

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.workspace import WorkspaceError
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


class _HistoryTool(Tool):
    effect_class = "read_only_internal"

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def _workspace(self) -> LocalWorkspacePort:
        configured = getattr(getattr(self, "_agent_config", None), "session_dir", None)
        root = Path(configured).expanduser() if configured else get_sessions_dir()
        root = root.resolve()
        return LocalWorkspacePort(root, cwd=root)

    @staticmethod
    def _session_path(session_id: str, suffix: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id):
            raise ValueError("session_id contains unsupported characters")
        return f"{session_id}/{suffix}"


@register_tool
class HistorySearchTool(_HistoryTool):
    name = "history_search"
    description = "Search the full append-only JSONL history of a saved session."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "pattern": {"type": "string", "description": "Regular expression"},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["session_id", "pattern"],
    }

    def execute(
        self, session_id: str, pattern: str, max_matches: int = 50
    ) -> ToolOutcome:
        return self.run_backend(
            session_id=session_id, pattern=pattern, max_matches=max_matches
        )

    @backend_handler("local")
    @backend_handler("remote_relay")
    def _execute_host(
        self, session_id: str, pattern: str, max_matches: int = 50
    ) -> ToolOutcome:
        try:
            regex = re.compile(pattern)
            path = self._session_path(session_id, "events.jsonl")
            lines = self._workspace().read_text(path).splitlines()
            matches = [
                f"{index}: {line}"
                for index, line in enumerate(lines, 1)
                if regex.search(line)
            ][: max(1, min(200, int(max_matches)))]
            return ToolOutcome(
                summary=f"Found {len(matches)} history matches in {session_id}",
                content="\n".join(matches) or "No matches found.",
                metadata={"session_id": session_id, "match_count": len(matches)},
                retention_hint=ToolRetentionHint(
                    strategy=ToolRetentionStrategy.HEAD_TAIL
                ),
            )
        except (ValueError, re.error, WorkspaceError, OSError) as error:
            return _failure(str(error))


@register_tool
class HistoryReadTool(_HistoryTool):
    name = "history_read"
    description = "Read a bounded sequence range from a saved session's JSONL ledger."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "start_seq": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "required": ["session_id"],
    }

    def execute(
        self, session_id: str, start_seq: int = 1, limit: int = 50
    ) -> ToolOutcome:
        return self.run_backend(session_id=session_id, start_seq=start_seq, limit=limit)

    @backend_handler("local")
    @backend_handler("remote_relay")
    def _execute_host(
        self, session_id: str, start_seq: int = 1, limit: int = 50
    ) -> ToolOutcome:
        try:
            path = self._session_path(session_id, "events.jsonl")
            selected: list[str] = []
            for line in self._workspace().read_text(path).splitlines():
                event = json.loads(line)
                if int(event.get("seq", 0)) < max(1, int(start_seq)):
                    continue
                selected.append(json.dumps(event, ensure_ascii=False, indent=2))
                if len(selected) >= max(1, min(200, int(limit))):
                    break
            return ToolOutcome(
                summary=f"Read {len(selected)} history events from {session_id}",
                content="\n".join(selected) or "No events in range.",
                metadata={"session_id": session_id, "event_count": len(selected)},
                retention_hint=ToolRetentionHint(strategy=ToolRetentionStrategy.HEAD),
            )
        except (ValueError, json.JSONDecodeError, WorkspaceError, OSError) as error:
            return _failure(str(error))


@register_tool
class ArtifactReadTool(_HistoryTool):
    name = "artifact_read"
    description = "Read an immutable artifact referenced by a session ledger event."
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "artifact_ref": {
                "type": "string",
                "description": "Path relative to the session artifacts directory",
            },
        },
        "required": ["session_id", "artifact_ref"],
    }

    def execute(self, session_id: str, artifact_ref: str) -> ToolOutcome:
        return self.run_backend(session_id=session_id, artifact_ref=artifact_ref)

    @backend_handler("local")
    @backend_handler("remote_relay")
    def _execute_host(self, session_id: str, artifact_ref: str) -> ToolOutcome:
        try:
            if Path(artifact_ref).is_absolute():
                raise ValueError("artifact_ref must be relative")
            path = self._session_path(session_id, f"artifacts/{artifact_ref}")
            content = self._workspace().read_text(path)
            return ToolOutcome(
                summary=f"Read artifact {artifact_ref} ({len(content)} chars)",
                content=content,
                metadata={"session_id": session_id, "artifact_ref": artifact_ref},
                retention_hint=ToolRetentionHint(strategy=ToolRetentionStrategy.HEAD),
            )
        except (ValueError, WorkspaceError, OSError) as error:
            return _failure(str(error))


def _failure(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=f"Error: {message}",
        error_kind=ToolErrorKind.EXECUTION,
    )
