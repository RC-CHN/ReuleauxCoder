"""Session domain models."""

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class SessionMetadata:
    """Metadata for a saved session."""

    id: str
    model: str
    saved_at: str
    preview: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMetadata":
        """Create from dictionary."""
        return cls(
            id=d.get("id", ""),
            model=d.get("model", "?"),
            saved_at=d.get("saved_at", "?"),
            preview=d.get("preview", ""),
        )


@dataclass
class Session:
    """A conversation session with messages and metadata."""

    id: str
    model: str
    saved_at: str
    messages: list[dict] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0

    @classmethod
    def create_new(cls, model: str) -> "Session":
        """Create a new session with auto-generated ID."""
        session_id = f"session_{int(time.time())}"
        return cls(
            id=session_id,
            model=model,
            saved_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            messages=[],
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        """Create from dictionary."""
        return cls(
            id=d.get("id", ""),
            model=d.get("model", "?"),
            saved_at=d.get("saved_at", "?"),
            messages=d.get("messages", []),
            total_prompt_tokens=d.get("total_prompt_tokens", 0),
            total_completion_tokens=d.get("total_completion_tokens", 0),
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "model": self.model,
            "saved_at": self.saved_at,
            "messages": self.messages,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
        }

    def get_preview(self) -> str:
        """Get preview text from first user message."""
        for m in self.messages:
            if m.get("role") == "user" and m.get("content"):
                return m["content"][:80]
        return ""
