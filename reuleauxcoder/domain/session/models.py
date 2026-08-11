"""Session domain models."""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from reuleauxcoder.domain.context.replay import ReplayEnvelope, RequestEnvelope
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.history import HistoryEvent
from reuleauxcoder.domain.llm.context_messages import is_synthetic_context_message

MAX_SESSION_PREVIEW_CHARS = 120


def is_safe_session_preview(value: object) -> bool:
    """Return whether a persisted preview is bounded and display-safe."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_SESSION_PREVIEW_CHARS
        and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)
    )


def normalize_session_preview(value: str) -> str:
    """Normalize message text into a bounded, single-line display preview."""
    without_controls = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in value
    )
    return " ".join(without_controls.split())[:MAX_SESSION_PREVIEW_CHARS]


def _display_message_text(message: dict) -> str:
    """Return user-facing conversation text without persistence markers."""
    if is_synthetic_context_message(message):
        return ""
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if message.get("role") == "user" and text.startswith("[SESSION_EXIT]"):
        return ""
    if message.get("role") == "user" and text.startswith("[SESSION_RESUME]"):
        _, separator, remainder = text.partition("\n\n")
        return remainder.strip() if separator else ""
    return text


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
    plan_state: dict[str, Any] = field(default_factory=dict)
    progress_state: dict[str, Any] = field(default_factory=dict)
    skills_disabled: list[str] = field(default_factory=list)

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
        plan_state = payload.get("plan_state")
        if not isinstance(plan_state, dict):
            plan_state = {}
        progress_state = payload.get("progress_state")
        if not isinstance(progress_state, dict):
            progress_state = {}
        skills_disabled = payload.get("skills_disabled")
        if not isinstance(skills_disabled, list):
            skills_disabled = []
        return cls(
            model=payload.get("model"),
            active_mode=payload.get("active_mode"),
            llm_debug_trace=payload.get("llm_debug_trace"),
            active_main_model_profile=payload.get("active_main_model_profile"),
            active_sub_model_profile=payload.get("active_sub_model_profile"),
            approval_rules=[
                dict(rule) for rule in approval_rules if isinstance(rule, dict)
            ],
            execution_target=payload.get("execution_target"),
            remote_binding=dict(remote_binding),
            plan_state=dict(plan_state),
            progress_state=dict(progress_state),
            skills_disabled=[str(name) for name in skills_disabled],
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize runtime state for persistence."""
        return asdict(self)


def _restore_fact(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"invalid restore issue {field_name}")
    if not value.isascii() or not value.replace("_", "").isalnum():
        raise ValueError(f"invalid restore issue {field_name}")
    return value


@dataclass(frozen=True, slots=True)
class SessionRestoreIssue:
    """Content-free facts for an optional artifact that restored degraded."""

    phase: str
    error_type: str
    ref: str
    count: int = 0

    def __post_init__(self) -> None:
        _restore_fact(self.phase, "phase")
        _restore_fact(self.error_type, "error_type")
        _restore_fact(self.ref, "ref")
        if (
            not isinstance(self.count, int)
            or isinstance(self.count, bool)
            or self.count < 0
        ):
            raise ValueError("restore issue count must be a non-negative integer")

    @classmethod
    def from_dict(cls, data: dict) -> "SessionRestoreIssue":
        if not isinstance(data, dict):
            raise TypeError("restore issue must be an object")
        phase = _restore_fact(data.get("phase"), "phase")
        error_type = _restore_fact(data.get("error_type"), "error_type")
        ref = _restore_fact(data.get("ref"), "ref")
        count = data.get("count", 0)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("restore issue count must be a non-negative integer")
        return cls(phase=phase, error_type=error_type, ref=ref, count=count)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "phase": self.phase,
            "error_type": self.error_type,
            "ref": self.ref,
        }
        if self.count:
            payload["count"] = self.count
        return payload

    def render(self) -> str:
        suffix = f", count={self.count}" if self.count else ""
        return (
            f"phase={self.phase}, error_type={self.error_type}, ref={self.ref}{suffix}"
        )


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
    history_events: list[HistoryEvent] = field(default_factory=list)
    replay_envelope: ReplayEnvelope | None = None
    request_envelopes: list[RequestEnvelope] = field(default_factory=list)
    checkpoints: list[CompactionCheckpoint] = field(default_factory=list)
    history_completeness: str = "legacy_compacted_or_unknown"
    history_next_seq_floor: int = 0
    history_behavior_projection_safe: bool = True
    restore_issues: tuple[SessionRestoreIssue, ...] = field(
        default_factory=tuple,
        repr=False,
    )

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        """Create from dictionary."""
        runtime_state = SessionRuntimeState.from_dict(d.get("runtime_state"))
        if runtime_state.model is None:
            runtime_state.model = d.get("model")
        if runtime_state.active_mode is None:
            runtime_state.active_mode = d.get("active_mode")
        replay_data = d.get("replay_envelope")
        request_data = d.get("request_envelopes") or []
        history_data = d.get("history_events") or []
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
            history_events=[
                HistoryEvent.from_dict(item)
                for item in history_data
                if isinstance(item, dict)
            ],
            replay_envelope=(
                ReplayEnvelope.from_dict(replay_data)
                if isinstance(replay_data, dict)
                else None
            ),
            request_envelopes=[
                RequestEnvelope(**item)
                for item in request_data
                if isinstance(item, dict)
            ],
            checkpoints=[
                CompactionCheckpoint.from_dict(item)
                for item in d.get("checkpoints", ())
                if isinstance(item, dict)
            ],
            history_completeness=str(
                d.get("history_completeness") or "legacy_compacted_or_unknown"
            ),
            restore_issues=tuple(
                SessionRestoreIssue.from_dict(item)
                for item in d.get("restore_issues", ())
            ),
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
            "history_completeness": self.history_completeness,
            "restore_issues": [issue.to_dict() for issue in self.restore_issues],
            "replay_envelope": (
                self.replay_envelope.to_dict() if self.replay_envelope else None
            ),
            "request_envelopes": [item.to_dict() for item in self.request_envelopes],
            "checkpoints": [item.to_dict() for item in self.checkpoints],
        }

    def get_preview(self) -> str:
        """Build a preview from the latest meaningful user request."""
        for message in reversed(self.messages):
            if message.get("role") != "user":
                continue
            text = normalize_session_preview(_display_message_text(message))
            if text:
                return text
        return ""

    def get_recent_conversation(self, max_user_turns: int = 3) -> list[dict[str, str]]:
        """Return a compact human transcript, excluding protocol/tool messages."""
        entries: list[dict[str, str]] = []
        for message in self.messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _display_message_text(message)
            if text:
                entries.append({"role": role, "content": text})

        user_positions = [
            index for index, entry in enumerate(entries) if entry["role"] == "user"
        ]
        if len(user_positions) > max_user_turns:
            entries = entries[user_positions[-max_user_turns] :]
        return entries
