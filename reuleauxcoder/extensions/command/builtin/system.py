"""Builtin system command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.help import build_help_view
from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.view_models import (
    HelpViewModel,
    PerformanceCategoryViewModel,
    PerformanceRowViewModel,
    PerformanceViewModel,
    TokenUsageViewModel,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    UI_TARGETS,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.runtime.session_state import (
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.runtime.effective_config import build_effective_config_view
from reuleauxcoder.domain.runtime.performance import PerformanceSample
from reuleauxcoder.infrastructure.fs.paths import get_diagnostics_dir


@dataclass(frozen=True, slots=True)
class ExitCommand:
    pass


@dataclass(frozen=True, slots=True)
class DebugCommand:
    enabled: bool | None


def _parse_help(user_input: str, parse_ctx):
    if match_template(user_input, "/help") is not None:
        return EmptyCommand()
    return None


def _parse_exit(user_input: str, parse_ctx):
    if matches_any(user_input, ("/quit", "/exit"), case_insensitive=True):
        return ExitCommand()
    return None


def _parse_reset(user_input: str, parse_ctx):
    if match_template(user_input, "/reset") is not None:
        return EmptyCommand()
    return None


def _parse_tokens(user_input: str, parse_ctx):
    if match_template(user_input, "/tokens") is not None:
        return EmptyCommand()
    return None


def _parse_config(user_input: str, parse_ctx):
    if match_template(user_input, "/config") is not None:
        return EmptyCommand()
    return None


def _parse_status_perf(user_input: str, parse_ctx):
    if matches_any(
        user_input,
        ("/status perf", "/debug performance"),
        case_insensitive=True,
    ):
        return EmptyCommand()
    return None


def _parse_debug(user_input: str, parse_ctx):
    if match_template(user_input, "/debug", case_insensitive=True) is not None:
        return DebugCommand(enabled=None)
    if match_template(user_input, "/debug on", case_insensitive=True) is not None:
        return DebugCommand(enabled=True)
    if match_template(user_input, "/debug off", case_insensitive=True) is not None:
        return DebugCommand(enabled=False)
    return None


def _handle_show_help(command, ctx) -> CommandEffect:
    if ctx.ui_profile is None:
        view = HelpViewModel(
            sections=(), diagnostic="No active UI profile; help unavailable."
        )
    elif ctx.action_registry is None:
        view = HelpViewModel(
            sections=(), diagnostic="No action registry available; help unavailable."
        )
    else:
        view = build_help_view(ctx.ui_profile, ctx.action_registry)
    ctx.effect.open_view(
        view,
        title="ReuleauxCoder Help",
        reuse_key="help",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_exit(command, ctx) -> CommandEffect:
    sid = ctx.exit_session()
    if sid is not None:
        ctx.effect.info(f"Session auto-saved: {sid}")
    return ctx.effect.finish(control="exit", session_id=ctx.agent.current_session_id)


def _handle_reset(command, ctx) -> CommandEffect:
    ctx.agent.reset()
    restore_config_runtime_defaults(ctx.config, ctx.agent)
    process_manager = getattr(ctx.agent, "process_manager", None)
    active_processes = (
        process_manager.active_count(owner_session_id=ctx.agent.current_session_id)
        if process_manager is not None
        else 0
    )
    process_note = (
        f" {active_processes} unresolved process session(s) were preserved; "
        "use /ps to inspect them."
        if active_processes
        else ""
    )
    ctx.effect.warning(
        "Conversation reset (in-memory only, does not delete saved sessions)."
        + process_note
    )
    return ctx.effect.finish(control="continue")


def _handle_tokens(command, ctx) -> CommandEffect:
    prompt_tokens = ctx.agent.state.total_prompt_tokens
    completion_tokens = ctx.agent.state.total_completion_tokens
    lifetime_total = prompt_tokens + completion_tokens

    current_context_tokens = ctx.agent.context.predict_request_tokens(
        ctx.agent.messages
    )
    max_context_tokens = ctx.agent.context.request_input_limit
    if max_context_tokens:
        context_ratio = current_context_tokens / max_context_tokens
        context_percent = round(context_ratio * 100, 1)
    else:
        context_percent = None

    observation = ctx.agent.context.latest_usage
    thresholds = ctx.agent.context.rewrite_thresholds

    view = TokenUsageViewModel(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        lifetime_total=lifetime_total,
        current_context_tokens=current_context_tokens,
        max_context_tokens=max_context_tokens,
        context_percent=context_percent,
        message_count=len(ctx.agent.messages),
        actual_prompt_tokens=(
            observation.actual_prompt_tokens if observation else None
        ),
        cached_input_tokens=(observation.cached_input_tokens if observation else None),
        automatic_strategies=ctx.agent.context.automatic_strategies,
        snip_wall=thresholds["snip_wall"],
        semantic_wall=thresholds["semantic_wall"],
        snip_min_gain=thresholds["snip_min_gain"],
        rewrite_target=thresholds["rewrite_target"],
        emergency_at=thresholds["emergency_at"],
        cache_epoch=ctx.agent.context.cache_epoch,
    )

    ctx.effect.open_view(
        view,
        title="Token Usage",
        reuse_key="token_usage",
    )

    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_debug(command, ctx) -> CommandEffect:
    enabled = (
        not bool(getattr(ctx.agent.llm, "debug_trace", False))
        if command.enabled is None
        else command.enabled
    )
    ctx.agent.llm.debug_trace = enabled
    if enabled:
        ctx.effect.info(
            "Detailed LLM request/response traces enabled for this session: "
            f"{get_diagnostics_dir()}. The session event ledger remains bounded."
        )
    else:
        ctx.effect.info(
            "Detailed LLM request/response traces disabled for this session."
        )
    return ctx.effect.finish(
        control="continue", state_changes={"llm_debug_trace": enabled}
    )


def _handle_config(command, ctx) -> CommandEffect:
    view = build_effective_config_view(ctx.config, ctx.agent)
    ctx.effect.open_view(
        view,
        title="Effective Configuration",
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _performance_row(sample: PerformanceSample) -> PerformanceRowViewModel:
    attributes = sample.attribute_map()
    detail_keys = (
        "hook_name",
        "tool_name",
        "server_name",
        "language",
        "root_hash",
        "transport_generation",
        "launcher",
        "work_kind",
        "request_kind",
        "sync_kind",
        "shutdown_phase",
        "cache_result",
        "cold_start",
        "document_committed",
        "document_version",
        "diagnostic_generation",
        "diagnostic_count",
        "transport_count",
        "respawn_count",
        "model",
        "tool_count",
        "event_count",
        "encoded_bytes",
        "fsync_ms",
        "attempt",
        "error_type",
        "outcome",
        "batch_size",
        "generation",
        "width",
        "cell_count",
        "batches",
        "cache_hits",
        "cache_misses",
        "render_rows",
        "depth",
        "high_watermark",
        "coalesced",
        "transient_dropped",
        "must_deliver_waits",
        "must_deliver_timeouts",
        "closed_dropped",
        "stale_generation_dropped",
        "stale_incident_dropped",
    )
    detail = " · ".join(
        f"{key}={attributes[key]}"
        for key in detail_keys
        if attributes.get(key) is not None
    )
    return PerformanceRowViewModel(
        sequence=sample.sequence,
        category=sample.category,
        operation=sample.name,
        elapsed_ms=sample.elapsed_ms,
        status=sample.status,
        detail=detail,
    )


def _handle_status_perf(command, ctx) -> CommandEffect:
    monitor = getattr(ctx.agent, "performance_monitor", None)
    samples = monitor.snapshot() if monitor is not None else ()
    grouped: dict[str, list[PerformanceSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.category, []).append(sample)
    categories = tuple(
        PerformanceCategoryViewModel(
            category=category,
            count=len(items),
            total_ms=round(sum(item.elapsed_ms for item in items), 3),
            max_ms=max(item.elapsed_ms for item in items),
            last_ms=items[-1].elapsed_ms,
        )
        for category, items in sorted(grouped.items())
    )
    recent = tuple(_performance_row(sample) for sample in reversed(samples[-20:]))
    slowest = tuple(
        _performance_row(sample)
        for sample in sorted(
            samples,
            key=lambda item: (item.elapsed_ms, item.sequence),
            reverse=True,
        )[:10]
    )
    view = PerformanceViewModel(
        retained_count=len(samples),
        capacity=getattr(monitor, "capacity", 0),
        dropped_count=getattr(monitor, "dropped", 0),
        categories=categories,
        recent=recent,
        slowest=slowest,
    )
    ctx.effect.open_view(
        view,
        title="Runtime Performance",
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="system.help",
                preview=True,
                command_type=EmptyCommand,
                feature_id="system",
                description="Show command help and scope annotations",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/help"),),
                parser=_parse_help,
                handler=_handle_show_help,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="system.exit",
                command_type=ExitCommand,
                audit="session_lifecycle",
                feature_id="system",
                description="Exit the interface after auto-saving the current session",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/quit"),),
                parser=_parse_exit,
                handler=_handle_exit,
            ),
            ActionSpec(
                action_id="system.reset",
                command_type=EmptyCommand,
                audit="session_lifecycle",
                feature_id="system",
                description="[session] Reset in-memory conversation and session runtime overrides",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/reset"),),
                parser=_parse_reset,
                handler=_handle_reset,
            ),
            ActionSpec(
                action_id="system.tokens",
                preview=True,
                command_type=EmptyCommand,
                feature_id="system",
                description="[session] Show token usage for the current session",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/tokens"),),
                parser=_parse_tokens,
                handler=_handle_tokens,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="system.debug",
                command_type=DebugCommand,
                audit="runtime_config_changed",
                feature_id="system",
                description="[session] Toggle detailed LLM request/response traces",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/debug"),
                    slash_trigger("/debug <on|off>"),
                ),
                parser=_parse_debug,
                handler=_handle_debug,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="system.status_perf",
                preview=True,
                command_type=EmptyCommand,
                feature_id="system",
                description="[session] Show recent runtime performance timings",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/status perf"),
                    slash_trigger("/debug performance"),
                ),
                parser=_parse_status_perf,
                handler=_handle_status_perf,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="system.config",
                preview=True,
                command_type=EmptyCommand,
                feature_id="system",
                description="Show effective configuration values, sources and diagnostics",
                ui_targets=UI_TARGETS,
                triggers=(slash_trigger("/config"),),
                parser=_parse_config,
                handler=_handle_config,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
        ]
    )
