"""Provider-neutral sub-agent control-plane models."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import time
import uuid


@dataclass(frozen=True, slots=True)
class AgentBudget:
    max_rounds: int = 20
    max_tool_calls: int = 80
    max_tokens: int | None = None
    timeout_seconds: int = 300
    max_depth: int = 1


@dataclass(slots=True)
class SubagentResult:
    status: str
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    confidence: str | None = None
    tool_uses: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_calls: int = 0
    duration_seconds: float = 0.0
    transcript_ref: str | None = None
    partial: bool = False
    worktree_path: str | None = None
    usage_uncertain: bool = False
    resume_ready: bool = False

    def canonical_payload(self) -> dict:
        """Return the stable model-visible result without wall-clock noise."""
        return {
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence,
            "files": self.files,
            "changes": self.changes,
            "unresolved": self.unresolved,
            "confidence": self.confidence,
            "usage": {
                "tool_uses": self.tool_uses,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "model_calls": self.model_calls,
            },
            "transcript_ref": self.transcript_ref,
            "partial": self.partial,
            "worktree_path": self.worktree_path,
            "usage_uncertain": self.usage_uncertain,
        }

    def content_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def model_text(self, *, max_chars: int = 6_000) -> str:
        payload = self.canonical_payload()
        payload["content_hash"] = self.content_hash()
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(encoded) <= max_chars:
            return encoded
        # Preserve the conclusion and artifact pointer instead of truncating a
        # raw transcript from the front.
        payload["evidence"] = self.evidence[:8]
        payload["files"] = self.files[:30]
        payload["changes"] = self.changes[:20]
        payload["unresolved"] = self.unresolved[:10]
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        return encoded[:max_chars]


class SubagentTranscriptStore:
    """Append-only transcripts used for inspection and resume."""

    def __init__(self, root: str | Path):
        self.root = Path(root) / ".rcoder" / "subagents"

    def write(self, job_id: str, messages: list[dict], metadata: dict) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{job_id}.json"
        stable = {
            "job_id": job_id,
            "saved_at": time.time(),
            "metadata": metadata,
            "messages": messages,
        }
        payload = {
            **stable,
            "content_hash": _checkpoint_hash(stable),
        }
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return str(path)

    def read(self, reference: str | Path) -> list[dict]:
        payload = json.loads(Path(reference).read_text(encoding="utf-8"))
        expected = payload.get("content_hash")
        if expected:
            stable = {
                key: value for key, value in payload.items() if key != "content_hash"
            }
            if _checkpoint_hash(stable) != expected:
                raise ValueError("subagent transcript checkpoint hash mismatch")
        messages = payload.get("messages", [])
        return [dict(item) for item in messages if isinstance(item, dict)]


def _checkpoint_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
