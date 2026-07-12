"""Actual-first provider usage observations for context planning."""

from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True, slots=True)
class UsageObservation:
    actual_prompt_tokens: int
    cached_input_tokens: int | None
    local_request_estimate: int
    local_history_estimate: int
    history_version: int
    request_boundary: str
    model_profile: str
    observed_at: float

    @classmethod
    def create(
        cls,
        *,
        actual_prompt_tokens: int,
        cached_input_tokens: int | None,
        local_request_estimate: int,
        local_history_estimate: int,
        history_version: int,
        request_boundary: str,
        model_profile: str,
    ) -> "UsageObservation":
        return cls(
            actual_prompt_tokens=max(0, int(actual_prompt_tokens)),
            cached_input_tokens=(
                max(0, int(cached_input_tokens))
                if cached_input_tokens is not None
                else None
            ),
            local_request_estimate=max(1, int(local_request_estimate)),
            local_history_estimate=max(0, int(local_history_estimate)),
            history_version=max(0, int(history_version)),
            request_boundary=request_boundary,
            model_profile=model_profile,
            observed_at=time.time(),
        )
