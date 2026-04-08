"""Session manager - compatibility wrappers over the persistence layer."""

from pathlib import Path
from typing import Optional, List, Tuple

from reuleauxcoder.domain.session.models import SessionMetadata
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def save_session(
    messages: list[dict],
    model: str,
    session_id: Optional[str] = None,
    sessions_dir: Optional[Path] = None,
    is_exit: bool = False,
) -> str:
    """Save conversation to disk. Returns the session ID."""
    return SessionStore(sessions_dir).save(
        messages=messages,
        model=model,
        session_id=session_id,
        is_exit=is_exit,
    )


def load_session(
    session_id: str,
    sessions_dir: Optional[Path] = None,
) -> Optional[Tuple[list[dict], str]]:
    """Load a saved session. Returns ``(messages, model)`` or ``None``."""
    return SessionStore(sessions_dir).load(session_id)


def get_exit_time(messages: list[dict]) -> Optional[str]:
    """Extract exit time from session messages, if present."""
    return SessionStore.get_exit_time(messages)


def list_sessions(
    sessions_dir: Optional[Path] = None,
    limit: int = 20,
) -> List[SessionMetadata]:
    """List available sessions, newest first."""
    return SessionStore(sessions_dir).list(limit=limit)


def get_latest_session(
    sessions_dir: Optional[Path] = None,
) -> Optional[SessionMetadata]:
    """Get the most recent session, or None if no sessions exist."""
    return SessionStore(sessions_dir).get_latest()
