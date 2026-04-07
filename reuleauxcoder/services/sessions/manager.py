"""Session manager - handles session persistence."""

import json
import time
from pathlib import Path
from typing import Optional, List, Tuple

from reuleauxcoder.domain.session.models import Session, SessionMetadata
from reuleauxcoder.infrastructure.fs.paths import get_sessions_dir


def save_session(
    messages: list[dict],
    model: str,
    session_id: Optional[str] = None,
    sessions_dir: Optional[Path] = None,
    is_exit: bool = False,
) -> str:
    """Save conversation to disk. Returns the session ID.
    
    Args:
        messages: List of conversation messages
        model: Model name
        session_id: Optional session ID (auto-generated if not provided)
        sessions_dir: Optional directory for session files
        is_exit: If True, append an exit marker message to record departure time
    """
    dir_path = sessions_dir or get_sessions_dir()
    dir_path.mkdir(parents=True, exist_ok=True)

    if not session_id:
        session_id = f"session_{int(time.time())}"

    # Copy messages to avoid mutating the original
    saved_messages = list(messages)
    
    # Append exit marker if this is a normal exit
    if is_exit:
        exit_time = time.strftime("%Y-%m-%d %H:%M:%S")
        saved_messages.append({
            "role": "system",
            "content": f"[SESSION_EXIT] User left the session at {exit_time}.",
        })

    data = {
        "id": session_id,
        "model": model,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": saved_messages,
    }

    path = dir_path / f"{session_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return session_id


def load_session(
    session_id: str,
    sessions_dir: Optional[Path] = None,
) -> Optional[Tuple[list[dict], str]]:
    """Load a saved session. Returns (messages, model) or None."""
    dir_path = sessions_dir or get_sessions_dir()
    path = dir_path / f"{session_id}.json"

    if not path.exists():
        return None

    data = json.loads(path.read_text())
    return data["messages"], data["model"]


def get_exit_time(messages: list[dict]) -> Optional[str]:
    """Extract exit time from session messages, if present.
    
    Returns the exit timestamp string, or None if no exit marker found.
    """
    for msg in reversed(messages):
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if content.startswith("[SESSION_EXIT]"):
                # Extract time from: "[SESSION_EXIT] User left the session at 2024-04-07 17:30:00."
                import re
                match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", content)
                if match:
                    return match.group(1)
    return None


def list_sessions(
    sessions_dir: Optional[Path] = None,
    limit: int = 20,
) -> List[SessionMetadata]:
    """List available sessions, newest first."""
    dir_path = sessions_dir or get_sessions_dir()

    if not dir_path.exists():
        return []

    sessions = []
    for f in sorted(dir_path.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            # Get first user message as preview
            preview = ""
            for m in data.get("messages", []):
                if m.get("role") == "user" and m.get("content"):
                    preview = m["content"][:80]
                    break
            sessions.append(
                SessionMetadata(
                    id=data.get("id", f.stem),
                    model=data.get("model", "?"),
                    saved_at=data.get("saved_at", "?"),
                    preview=preview,
                )
            )
        except (json.JSONDecodeError, KeyError):
            continue

    return sessions[:limit]


def get_latest_session(
    sessions_dir: Optional[Path] = None,
) -> Optional[SessionMetadata]:
    """Get the most recent session, or None if no sessions exist."""
    sessions = list_sessions(sessions_dir, limit=1)
    return sessions[0] if sessions else None
