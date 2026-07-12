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
    cache_epoch: int = 0
    actual_prompt_tokens: int | None = None
    cached_input_tokens: int | None = None
    invalidated_suffix_tokens: int | None = None
    reclaimed_tokens: int | None = None

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
        cache_epoch: int = 0,
        actual_prompt_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        invalidated_suffix_tokens: int | None = None,
        reclaimed_tokens: int | None = None,
    ) -> "CompactionCheckpoint":
        return cls(
            id=f"cc_{uuid.uuid4().hex[:12]}",
            trigger=trigger,
            strategy=tuple(strategy),
            source_history_version=source_history_version,
            replacement_history=tuple(
                {key: value for key, value in item.items() if key != "_rc_token_count"}
                for item in replacement_history
            ),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            preserved_rounds=preserved_rounds,
            created_at=time.time(),
            cache_epoch=cache_epoch,
            actual_prompt_tokens=actual_prompt_tokens,
            cached_input_tokens=cached_input_tokens,
            invalidated_suffix_tokens=invalidated_suffix_tokens,
            reclaimed_tokens=reclaimed_tokens,
        )
