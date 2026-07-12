"""Provider-neutral sub-agent control-plane models."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time


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
    duration_seconds: float = 0.0
    transcript_ref: str | None = None
    partial: bool = False
    worktree_path: str | None = None

    def model_text(self, *, max_chars: int = 6_000) -> str:
        payload = {
            "status": self.status,
            "summary": self.summary,
            "evidence": self.evidence,
            "files": self.files,
            "changes": self.changes,
            "unresolved": self.unresolved,
            "confidence": self.confidence,
            "usage": {
                "tool_uses": self.tool_uses,
                "duration_seconds": round(self.duration_seconds, 3),
            },
            "transcript_ref": self.transcript_ref,
            "partial": self.partial,
            "worktree_path": self.worktree_path,
        }
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
        payload = {
            "job_id": job_id,
            "saved_at": time.time(),
            "metadata": metadata,
            "messages": messages,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def read(self, reference: str | Path) -> list[dict]:
        payload = json.loads(Path(reference).read_text(encoding="utf-8"))
        messages = payload.get("messages", [])
        return [dict(item) for item in messages if isinstance(item, dict)]
