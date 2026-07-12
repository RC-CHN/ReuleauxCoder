"""Versioned compaction checkpoints."""

from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CompactionCheckpoint":
        return cls(
            id=str(data["id"]),
            trigger=str(data["trigger"]),
            strategy=tuple(str(item) for item in data.get("strategy", ())),
            source_history_version=int(data.get("source_history_version", 0)),
            replacement_history=tuple(
                dict(item) for item in data.get("replacement_history", ())
            ),
            tokens_before=int(data.get("tokens_before", 0)),
            tokens_after=int(data.get("tokens_after", 0)),
            preserved_rounds=int(data.get("preserved_rounds", 0)),
            created_at=float(data.get("created_at", 0.0)),
            cache_epoch=int(data.get("cache_epoch", 0)),
            actual_prompt_tokens=_optional_int(data.get("actual_prompt_tokens")),
            cached_input_tokens=_optional_int(data.get("cached_input_tokens")),
            invalidated_suffix_tokens=_optional_int(
                data.get("invalidated_suffix_tokens")
            ),
            reclaimed_tokens=_optional_int(data.get("reclaimed_tokens")),
        )

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


def _optional_int(value) -> int | None:
    return None if value is None else int(value)
