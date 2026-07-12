"""Provider extension boundary for cache-aware context compaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProviderCompactionResult:
    messages: list[dict]
    token_credit: int = 0
    metadata: dict | None = None


class ProviderContextCompactor(Protocol):
    """Optional provider primitive; core policy never depends on its API."""

    def compact_tool_results(
        self, messages: list[dict], *, keep_recent_rounds: int
    ) -> ProviderCompactionResult | None: ...


class NoopProviderContextCompactor:
    def compact_tool_results(
        self, messages: list[dict], *, keep_recent_rounds: int
    ) -> ProviderCompactionResult | None:
        del messages, keep_recent_rounds
        return None
