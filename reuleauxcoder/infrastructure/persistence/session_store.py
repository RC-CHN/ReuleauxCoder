"""Session persistence adapter backed by JSON files."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.session.models import Session, SessionMetadata
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir


class SessionStore:
    """File-backed store for conversation sessions."""

    def __init__(self, sessions_dir: Path | None = None):
        self._sessions_dir = sessions_dir or get_sessions_dir()

    @property
    def sessions_dir(self) -> Path:
        """Return the underlying session directory."""
        return self._sessions_dir

    def save(
        self,
        messages: list[dict],
        model: str,
        session_id: Optional[str] = None,
        is_exit: bool = False,
    ) -> str:
        """Save conversation to disk and return the session ID."""
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

        if not session_id:
            session_id = f"session_{int(time.time())}"

        saved_messages = list(messages)
        if is_exit:
            exit_time = time.strftime("%Y-%m-%d %H:%M:%S")
            saved_messages.append(
                {
                    "role": "system",
                    "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
                }
            )

        session = Session(
            id=session_id,
            model=model,
            saved_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            messages=saved_messages,
        )
        path = self._get_session_path(session_id)
        path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
        return session_id

    def load(self, session_id: str) -> tuple[list[dict], str] | None:
        """Load a saved session and return ``(messages, model)``."""
        path = self._get_session_path(session_id)
        if not path.exists():
            return None

        data = json.loads(path.read_text())
        session = Session.from_dict(data)
        return session.messages, session.model

    def list(self, limit: int = 20) -> list[SessionMetadata]:
        """List available sessions, newest first."""
        if not self._sessions_dir.exists():
            return []

        sessions: list[SessionMetadata] = []
        for file_path in sorted(self._sessions_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(file_path.read_text())
                session = Session.from_dict(data)
                sessions.append(
                    SessionMetadata(
                        id=session.id or file_path.stem,
                        model=session.model,
                        saved_at=session.saved_at,
                        preview=session.get_preview(),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue

        return sessions[:limit]

    def get_latest(self) -> SessionMetadata | None:
        """Return the most recent session metadata, if any."""
        sessions = self.list(limit=1)
        return sessions[0] if sessions else None

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        """Extract exit time from persisted session messages, if present."""
        for msg in reversed(messages):
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "")
            if not content.startswith("[SESSION_EXIT]"):
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", content)
            if match:
                return match.group(1)
        return None

    def _get_session_path(self, session_id: str) -> Path:
        """Return the JSON file path for a session ID."""
        return self._sessions_dir / f"{session_id}.json"
