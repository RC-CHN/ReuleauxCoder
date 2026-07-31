"""Passive text fallbacks for structured TUI view events."""

from __future__ import annotations

import json

from reuleauxcoder.app.commands.view_models import (
    EffectiveConfigViewModel,
    HelpViewModel,
    PerformanceViewModel,
    SessionResumeViewModel,
    SessionsViewModel,
    SubagentJobsViewModel,
    ThinkingEffortViewModel,
    TokenUsageViewModel,
)
from reuleauxcoder.interfaces.events import ViewEventPayload
from reuleauxcoder.interfaces.tui.formatting import clip


def format_help_view(model: HelpViewModel) -> str:
    lines: list[str] = []
    for section in model.sections:
        lines.append(f"[{section.feature_id}]")
        width = max((len(command.usage) for command in section.commands), default=0)
        for command in section.commands:
            lines.append(f"  {command.usage.ljust(width)}  {command.description}")
    if model.diagnostic:
        lines.append(f"! {model.diagnostic}")
    return "\n".join(lines) or "(no commands available)"


def format_thinking_effort_view(model: ThinkingEffortViewModel) -> str:
    lines = [f"Reasoning effort: {model.current}", f"Parameter: {model.param}", "Available:"]
    for level in model.levels:
        marker = " ✓" if level.label == model.current else ""
        lines.append(f"  {level.label} → {level.api_value}{marker}")
    lines.append(f"(profile default: {model.profile_default})")
    return "\n".join(lines)


def format_token_usage_view(model: TokenUsageViewModel) -> str:
    lines = [
        "Tokens · "
        f"prompt {model.prompt_tokens:,} · "
        f"completion {model.completion_tokens:,} · "
        f"lifetime {model.lifetime_total:,}"
    ]
    if model.max_context_tokens:
        ratio = model.current_context_tokens / model.max_context_tokens
        filled = round(max(0.0, min(1.0, ratio)) * 10)
        bar = "█" * filled + "·" * (10 - filled)
        percent = (
            f"{model.context_percent:.0f}%"
            if model.context_percent is not None
            else f"{ratio * 100:.0f}%"
        )
        lines.append(
            f"Context [{bar}] {percent} "
            f"({model.current_context_tokens:,} / {model.max_context_tokens:,})"
            f" · {model.message_count} messages"
        )
    if model.actual_prompt_tokens is not None:
        cached = (
            f" · cached {model.cached_input_tokens:,}"
            if model.cached_input_tokens
            else ""
        )
        lines.append(f"Actual  prompt {model.actual_prompt_tokens:,}{cached}")
    lines.append(
        f"Walls   snip {model.snip_wall}% · semantic {model.semantic_wall}%"
        f" · min-gain {model.snip_min_gain}% · target {model.rewrite_target}%"
        f" · emergency {model.emergency_at}% · epoch {model.cache_epoch}"
    )
    return "\n".join(lines)


def format_runtime_performance_view(model: PerformanceViewModel) -> str:
    lines = [
        f"Performance · retained {model.retained_count}/{model.capacity}"
        f" · overwritten {model.dropped_count}"
    ]
    for category in model.categories:
        lines.append(
            f"{category.category:<12} count {category.count:<3}"
            f" · max {category.max_ms:.1f} ms"
            f" · last {category.last_ms:.1f} ms"
        )
    if model.slowest:
        lines.append("Slowest:")
        for row in model.slowest[:5]:
            detail = f" · {row.detail}" if row.detail else ""
            lines.append(
                f"  {row.category}/{row.operation}: {row.elapsed_ms:.1f} ms"
                f" [{row.status}]{detail}"
            )
    if model.recent:
        lines.append("Recent:")
        for row in model.recent[:5]:
            lines.append(
                f"  #{row.sequence} {row.category}/{row.operation}:"
                f" {row.elapsed_ms:.1f} ms [{row.status}]"
            )
    if not model.categories:
        lines.append("(no runtime performance samples yet)")
    return "\n".join(lines)


def format_subagent_jobs_view(model: SubagentJobsViewModel) -> str:
    lines = [
        f"Agents · parallel {model.runtime_parallel_explore}"
        f"/{model.max_parallel_explore}"
    ]
    if not model.jobs:
        lines.append("(no jobs yet)")
    for job in model.jobs:
        lines.append(f"{job.job_id}  {job.status:<9} {job.mode:<8} {clip(job.task, 60)}")
    return "\n".join(lines)


def format_sessions_view(model: SessionsViewModel) -> str:
    scope = "all fingerprints" if model.show_all else f"fingerprint {model.fingerprint}"
    lines = [f"Sessions ({scope})"]
    if not model.sessions:
        lines.append("(no saved sessions)")
    for session in model.sessions:
        position = f"#{session.position}" if session.position is not None else "  "
        active = "  [active]" if session.active else ""
        lines.append(
            f"{position} {session.saved_at[:19]} · {session.model}"
            f" · {clip(session.preview, 50)}{active}"
        )
    return "\n".join(lines)


def format_effective_config_view(model: EffectiveConfigViewModel) -> str:
    lines = [f"{row.path} = {row.value}  ({row.source})" for row in model.rows]
    for diagnostic in model.diagnostics:
        lines.append(f"! {diagnostic}")
    return "\n".join(lines) or "(no configuration rows)"


def view_text(payload: ViewEventPayload) -> str:
    """Project a structured view to passive transcript text."""
    model = payload.view_model
    if payload.view_type == "session_resume" and isinstance(
        model, SessionResumeViewModel
    ):
        lines = [f"RESTORED {model.session_id} · {model.model} · {model.saved_at[:19]}"]
        lines.extend(
            f"{'YOU' if entry.role == 'user' else 'AGENT'}  {entry.content}"
            for entry in model.entries
        )
        return "\n".join(lines)
    if payload.view_type == "help" and isinstance(model, HelpViewModel):
        return format_help_view(model)
    if payload.view_type == "thinking_effort" and isinstance(
        model, ThinkingEffortViewModel
    ):
        return format_thinking_effort_view(model)
    if payload.view_type == "token_usage" and isinstance(model, TokenUsageViewModel):
        return format_token_usage_view(model)
    if payload.view_type == "runtime_performance" and isinstance(
        model, PerformanceViewModel
    ):
        return format_runtime_performance_view(model)
    if payload.view_type == "subagent_jobs" and isinstance(
        model, SubagentJobsViewModel
    ):
        return format_subagent_jobs_view(model)
    if payload.view_type == "sessions" and isinstance(model, SessionsViewModel):
        return format_sessions_view(model)
    if payload.view_type == "effective_config" and isinstance(
        model, EffectiveConfigViewModel
    ):
        return format_effective_config_view(model)
    to_payload = getattr(model, "to_payload", None)
    if callable(to_payload):
        try:
            return json.dumps(to_payload(), ensure_ascii=False, indent=2)
        except Exception:
            pass
    to_dict = getattr(model, "to_dict", None)
    if callable(to_dict):
        try:
            return json.dumps(to_dict(), ensure_ascii=False, indent=2)
        except Exception:
            pass
    return f"{payload.title}: {model}"


__all__ = [
    "format_effective_config_view",
    "format_help_view",
    "format_runtime_performance_view",
    "format_sessions_view",
    "format_subagent_jobs_view",
    "format_thinking_effort_view",
    "format_token_usage_view",
    "view_text",
]
