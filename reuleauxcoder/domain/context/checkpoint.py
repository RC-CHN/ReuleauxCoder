"""Versioned compaction checkpoints."""

from dataclasses import dataclass
import time
import uuid


@dataclass(frozen=True, slots=True)
class CompactionCheckpoint:
    id: str
    trigger: str
    strategy: tuple[str, ...]
    source_history_version: int
    replacement_history: tuple[dict, ...]
    tokens_before: int
    tokens_after: int
    preserved_rounds: int
    created_at: float

    @classmethod
    def create(
        cls,
        *,
        trigger: str,
        strategy: list[str],
        source_history_version: int,
        replacement_history: list[dict],
        tokens_before: int,
        tokens_after: int,
        preserved_rounds: int,
    ) -> "CompactionCheckpoint":
        return cls(
            id=f"cc_{uuid.uuid4().hex[:12]}",
            trigger=trigger,
            strategy=tuple(strategy),
            source_history_version=source_history_version,
            replacement_history=tuple(dict(item) for item in replacement_history),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            preserved_rounds=preserved_rounds,
            created_at=time.time(),
        )
