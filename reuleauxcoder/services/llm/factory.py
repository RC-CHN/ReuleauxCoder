"""Helpers for constructing/reconfiguring LLM clients from config/profile settings."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.services.llm.client import LLM


_LLM_RUNTIME_FIELDS = (
    "model",
    "api_key",
    "base_url",
    "temperature",
    "max_tokens",
    "preserve_reasoning_content",
    "backfill_reasoning_content_for_tool_calls",
    "reasoning_effort",
    "thinking_enabled",
    "reasoning_replay_mode",
    "reasoning_replay_placeholder",
)


def llm_runtime_kwargs(settings: Any, *, debug_trace: bool = False) -> dict[str, Any]:
    """Extract LLM constructor/reconfigure kwargs from a config/profile-like object."""
    kwargs = {field: getattr(settings, field) for field in _LLM_RUNTIME_FIELDS}
    kwargs["debug_trace"] = debug_trace
    return kwargs


def build_llm_from_settings(settings: Any, *, debug_trace: bool = False) -> LLM:
    """Create an LLM from a config/profile-like object."""
    return LLM(**llm_runtime_kwargs(settings, debug_trace=debug_trace))


def reconfigure_llm_from_settings(
    llm: LLM, settings: Any, *, debug_trace: bool | None = None
) -> None:
    """Reconfigure an existing LLM from a config/profile-like object."""
    kwargs = llm_runtime_kwargs(
        settings,
        debug_trace=llm.debug_trace if debug_trace is None else debug_trace,
    )
    llm.reconfigure(**kwargs)
