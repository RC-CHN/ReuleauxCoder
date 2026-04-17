"""Session domain models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SessionMetadata:
    """Metadata for a saved session."""

    id: str
    model: str
    saved_at: str
    preview: str = ""
    fingerprint: str = "local"

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMetadata":
        """Create from dictionary."""
        return cls(
            id=d.get("id", ""),
            model=d.get("model", "?"),
            saved_at=d.get("saved_at", "?"),
            preview=d.get("preview", ""),
            fingerprint=d.get("fingerprint", "local"),
        )


@dataclass
class SessionRuntimeState:
    """Session-scoped runtime overrides layered on top of config defaults."""

    model: str | None = None
    active_mode: str | None = None
    llm_debug_trace: bool | None = None
    active_main_model_profile: str | None = None
    active_sub_model_profile: str | None = None
    approval_rules: list[dict[str, Any]] = field(default_factory=list)
    execution_target: str | None = None
    remote_binding: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionRuntimeState":
        """Create runtime state from persisted dictionary data."""
        payload = data or {}
        remote_binding = payload.get("remote_binding")
        if not isinstance(remote_binding, dict):
            remote_binding = {}
        approval_rules = payload.get("approval_rules")
        if not isinstance(approval_rules, list):
            approval_rules = []
        return cls(
            model=payload.get("model"),
            active_mode=payload.get("active_mode"),
            llm_debug_trace=payload.get("llm_debug_trace"),
            active_main_model_profile=payload.get("active_main_model_profile"),
            active_sub_model_profile=payload.get("active_sub_model_profile"),
            approval_rules=[dict(rule) for rule in approval_rules if isinstance(rule, dict)],
            execution_target=payload.get("execution_target"),
            remote_binding=dict(remote_binding),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize runtime state for persistence."""
        return asdict(self)


@dataclass
class Session:
    """A conversation session with messages and metadata."""

    id: str
    model: str
    saved_at: str
    fingerprint: str = "local"
    messages: list[dict] = field(default_factory=list)
    active_mode: str | None = None
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    runtime_state: SessionRuntimeState = field(default_factory=SessionRuntimeState)

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        """Create from dictionary."""
        runtime_state = SessionRuntimeState.from_dict(d.get("runtime_state"))
        if runtime_state.model is None:
            runtime_state.model = d.get("model")
        if runtime_state.active_mode is None:
            runtime_state.active_mode = d.get("active_mode")
        return cls(
            id=d.get("id", ""),
            model=d.get("model", runtime_state.model or "?"),
            saved_at=d.get("saved_at", "?"),
            fingerprint=d.get("fingerprint", "local"),
            messages=d.get("messages", []),
            active_mode=d.get("active_mode") or runtime_state.active_mode,
            total_prompt_tokens=d.get("total_prompt_tokens", 0),
            total_completion_tokens=d.get("total_completion_tokens", 0),
            runtime_state=runtime_state,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "model": self.model,
            "saved_at": self.saved_at,
            "fingerprint": self.fingerprint,
            "messages": self.messages,
            "active_mode": self.active_mode,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "runtime_state": self.runtime_state.to_dict(),
        }

    def get_preview(self) -> str:
        """Get preview text from first user message."""
        for m in self.messages:
            if m.get("role") == "user" and m.get("content"):
                return m["content"][:80]
        return ""
