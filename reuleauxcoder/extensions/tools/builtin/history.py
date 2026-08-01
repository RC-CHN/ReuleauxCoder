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
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


ARTIFACT_READ_DEFAULT_CHARS = 12_000
ARTIFACT_READ_MAX_CHARS = 12_000


class _HistoryTool(Tool):
    effect_class = "read_only_internal"
    parallel_safe = True

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None

    def bind_agent(self, agent) -> None:
        self._agent = agent

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

    def _resolve_session_id(self, explicit_session_id: str | None) -> str:
        if explicit_session_id is not None:
            return explicit_session_id
        current_session_id = getattr(self._agent, "current_session_id", None)
        if not isinstance(current_session_id, str) or not current_session_id:
            raise ValueError(
                "current session is unavailable; provide session_id explicitly"
            )
        return current_session_id


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


class ArtifactReadTool(_HistoryTool):
    name = "artifact_read"
    description = (
        "Read one bounded page of an immutable artifact. The current session is "
        "used by default; provide session_id only to read another saved session. "
        "Use next_offset from the result to continue."
    )
    parameters = {
        "type": "object",
        "properties": {
            "artifact_ref": {
                "type": "string",
                "minLength": 1,
                "description": "Path relative to the session artifacts directory",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "Zero-based character offset. Default 0.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": ARTIFACT_READ_MAX_CHARS,
                "description": (
                    f"Maximum characters to return. Default and hard maximum "
                    f"{ARTIFACT_READ_MAX_CHARS}."
                ),
            },
            "session_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Saved session ID. Omit to use the current active session."
                ),
            },
        },
        "required": ["artifact_ref"],
        "additionalProperties": False,
    }

    def execute(
        self,
        artifact_ref: str,
        offset: int = 0,
        limit: int = ARTIFACT_READ_DEFAULT_CHARS,
        session_id: str | None = None,
    ) -> ToolOutcome:
        return self.run_backend(
            artifact_ref=artifact_ref,
            offset=offset,
            limit=limit,
            session_id=session_id,
        )

    @backend_handler("local")
    @backend_handler("remote_relay")
    def _execute_host(
        self,
        artifact_ref: str,
        offset: int = 0,
        limit: int = ARTIFACT_READ_DEFAULT_CHARS,
        session_id: str | None = None,
    ) -> ToolOutcome:
        try:
            if Path(artifact_ref).is_absolute():
                raise ValueError("artifact_ref must be relative")
            if (
                not isinstance(offset, int)
                or isinstance(offset, bool)
                or offset < 0
            ):
                raise ValueError("offset must be a non-negative integer")
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= ARTIFACT_READ_MAX_CHARS
            ):
                raise ValueError(
                    f"limit must be an integer from 1 to {ARTIFACT_READ_MAX_CHARS}"
                )

            resolved_session_id = self._resolve_session_id(session_id)
            path = self._session_path(
                resolved_session_id, f"artifacts/{artifact_ref}"
            )
            content = self._workspace().read_text(path)
            page = content[offset : offset + limit]
            end_offset = offset + len(page)
            next_offset = end_offset if end_offset < len(content) else None
            range_summary = (
                f"chars [{offset}:{end_offset}] of {len(content)}"
            )
            if next_offset is None:
                continuation = "Artifact read complete."
            else:
                session_argument = (
                    f", session_id={json.dumps(session_id)}"
                    if session_id is not None
                    else ""
                )
                continuation = (
                    f"Next offset: {next_offset}. Continue with "
                    f"artifact_read(artifact_ref={json.dumps(artifact_ref)}, "
                    f"offset={next_offset}, limit={limit}{session_argument})."
                )
            model_content = (
                f"[artifact page: {artifact_ref}; {range_summary}]\n"
                f"{page}\n"
                f"[{continuation}]"
            )
            return ToolOutcome(
                summary=f"Read artifact {artifact_ref}, {range_summary}",
                content=page,
                model_content=model_content,
                metadata={
                    "session_id": resolved_session_id,
                    "artifact_ref": artifact_ref,
                    "offset": offset,
                    "limit": limit,
                    "returned_chars": len(page),
                    "total_chars": len(content),
                    "next_offset": next_offset,
                    "complete": next_offset is None,
                },
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
