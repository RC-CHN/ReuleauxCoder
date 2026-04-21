"""Session persistence adapter backed by JSON files."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from reuleauxcoder.domain.context.manager import ensure_message_token_counts
from reuleauxcoder.domain.session.models import Session, SessionMetadata, SessionRuntimeState
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir

DEFAULT_SESSION_FINGERPRINT = "local"


class SessionStore:
    """File-backed store for conversation sessions."""

    def __init__(self, sessions_dir: Path | None = None):
        self._sessions_dir = sessions_dir or get_sessions_dir()
        self._lock = threading.RLock()

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
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> str:
        """Save conversation to disk and return the session ID."""
        with self._lock:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)

            if not session_id:
                session_id = self.generate_session_id()

            saved_messages = [dict(message) for message in messages]
            ensure_message_token_counts(saved_messages)
            if is_exit:
                exit_time = time.strftime("%Y-%m-%d %H:%M:%S")
                exit_message = {
                    "role": "system",
                    "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
                }
                ensure_message_token_counts([exit_message])
                saved_messages.append(exit_message)

            effective_runtime = runtime_state or SessionRuntimeState(model=model, active_mode=active_mode)
            if effective_runtime.model is None:
                effective_runtime.model = model
            if effective_runtime.active_mode is None:
                effective_runtime.active_mode = active_mode

            session = Session(
                id=session_id,
                model=effective_runtime.model or model,
                saved_at=datetime.now().isoformat(timespec="microseconds"),
                fingerprint=fingerprint or DEFAULT_SESSION_FINGERPRINT,
                messages=saved_messages,
                active_mode=effective_runtime.active_mode or active_mode,
                total_prompt_tokens=total_prompt_tokens,
                total_completion_tokens=total_completion_tokens,
                runtime_state=effective_runtime,
            )
            path = self._get_session_path(session_id)
            path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
            return session_id

    def append_system_message(
        self,
        session_id: str,
        model: str,
        content: str,
        *,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = DEFAULT_SESSION_FINGERPRINT,
    ) -> None:
        """Append a system message to an existing session, creating it if needed."""
        with self._lock:
            loaded = self.load(session_id)
            if loaded is None:
                self.save(
                    messages=[{"role": "system", "content": content}],
                    model=model,
                    session_id=session_id,
                    active_mode=active_mode,
                    runtime_state=runtime_state,
                    fingerprint=fingerprint,
                )
                return

            updated_messages = list(loaded.messages)
            updated_messages.append({"role": "system", "content": content})
            self.save(
                messages=updated_messages,
                model=loaded.model or model,
                session_id=session_id,
                total_prompt_tokens=loaded.total_prompt_tokens,
                total_completion_tokens=loaded.total_completion_tokens,
                active_mode=loaded.active_mode or active_mode,
                runtime_state=runtime_state or loaded.runtime_state,
                fingerprint=loaded.fingerprint or fingerprint,
            )

    @staticmethod
    def generate_session_id() -> str:
        """Generate a new session ID."""
        return f"session_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def load(self, session_id: str) -> Session | None:
        """Load a saved session."""
        with self._lock:
            path = self._get_session_path(session_id)
            if not path.exists():
                return None

            data = json.loads(path.read_text())
            session = Session.from_dict(data)
            updated_messages = [dict(message) for message in session.messages]
            ensure_message_token_counts(updated_messages)
            session.messages = updated_messages
            if session.runtime_state.model is None:
                session.runtime_state.model = session.model
            if session.runtime_state.active_mode is None:
                session.runtime_state.active_mode = session.active_mode
            if updated_messages != data.get("messages"):
                path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
            return session

    def list(
        self,
        limit: int = 20,
        *,
        fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT,
    ) -> list[SessionMetadata]:
        """List available sessions, newest first."""
        with self._lock:
            if not self._sessions_dir.exists():
                return []

            ranked_sessions: list[tuple[tuple[int, datetime, str], SessionMetadata]] = []
            for file_path in self._sessions_dir.glob("*.json"):
                try:
                    data = json.loads(file_path.read_text())
                    session = Session.from_dict(data)
                    if fingerprint is not None and session.fingerprint != fingerprint:
                        continue

                    metadata = SessionMetadata(
                        id=session.id or file_path.stem,
                        model=session.model,
                        saved_at=session.saved_at,
                        preview=session.get_preview(),
                        fingerprint=session.fingerprint,
                    )

                    stat = file_path.stat()
                    try:
                        saved_at_rank = datetime.fromisoformat(session.saved_at)
                    except (TypeError, ValueError):
                        try:
                            saved_at_rank = datetime.strptime(session.saved_at, "%Y-%m-%d %H:%M:%S")
                        except (TypeError, ValueError):
                            saved_at_rank = datetime.fromtimestamp(0)

                    ranked_sessions.append(((stat.st_mtime_ns, saved_at_rank, metadata.id), metadata))
                except (json.JSONDecodeError, KeyError):
                    continue

            ranked_sessions.sort(key=lambda item: item[0], reverse=True)
            return [metadata for _, metadata in ranked_sessions[:limit]]

    def get_latest(self, *, fingerprint: str | None = DEFAULT_SESSION_FINGERPRINT) -> SessionMetadata | None:
        """Return the most recent session metadata, if any."""
        sessions = self.list(limit=1, fingerprint=fingerprint)
        return sessions[0] if sessions else None

    @staticmethod
    def get_exit_time(messages: list[dict]) -> str | None:
        """Extract exit time from persisted session messages, if present."""
        for msg in reversed(messages):
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "") or ""
            match = re.search(r"\[SESSION_EXIT\].* at (.+?)\.$", content)
            if match:
                return match.group(1)
        return None

    def _get_session_path(self, session_id: str) -> Path:
        """Map session ID to JSON file path."""
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        return self._sessions_dir / f"{safe_id}.json"
