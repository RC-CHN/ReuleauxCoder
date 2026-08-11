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
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    enum_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.runtime.session_state import (
    build_session_persistence_kwargs,
    build_session_runtime_state,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.runtime.effective_config import build_effective_config_view
from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.domain.runtime.performance import PerformanceSample
from reuleauxcoder.infrastructure.fs.paths import get_diagnostics_dir
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore

_FORCE_COMPACT_STRATEGIES = {"snip", "summarize", "collapse"}


@dataclass(frozen=True, slots=True)
class ExitCommand:
    current_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompactContextCommand:
    force_strategy: str | None = None


@dataclass(frozen=True, slots=True)
class DebugCommand:
    enabled: bool | None


def _parse_help(user_input: str, parse_ctx):
    if match_template(user_input, "/help") is not None:
        return EmptyCommand()
    return None


def _parse_exit(user_input: str, parse_ctx):
    if matches_any(user_input, ("/quit", "/exit"), case_insensitive=True):
        return ExitCommand(current_session_id=parse_ctx.current_session_id)
    return None


def _parse_reset(user_input: str, parse_ctx):
    if match_template(user_input, "/reset") is not None:
        return EmptyCommand()
    return None


def _parse_compact(user_input: str, parse_ctx):
    if match_template(user_input, "/compact") is not None:
        return CompactContextCommand()

    captures = match_template(
        user_input, "/compact force {strategy}", case_insensitive=True
    )
    if captures is None:
        return None

    try:
        strategy = enum_text(_FORCE_COMPACT_STRATEGIES, case_insensitive=True).parse(
            captures["strategy"]
        )
    except ParamParseError:
        return CompactContextCommand(force_strategy="")

    return CompactContextCommand(force_strategy=strategy)


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
        view.view_type,
        title="ReuleauxCoder Help",
        view_model=view,
        reuse_key="help",
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_exit(command, ctx) -> CommandEffect:
    if ctx.agent.messages and ctx.config.session_auto_save:
        sid = SessionStore(ctx.sessions_dir).save(
            ctx.agent.messages,
            getattr(ctx.agent.llm, "model", ctx.config.model),
            command.current_session_id,
            is_exit=True,
            total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
            total_completion_tokens=ctx.agent.state.total_completion_tokens,
            active_mode=getattr(ctx.agent, "active_mode", None),
            runtime_state=build_session_runtime_state(ctx.config, ctx.agent),
            fingerprint=get_session_fingerprint(ctx.config, ctx.agent),
            incremental=True,
            events_already_persisted=True,
            **build_session_persistence_kwargs(ctx.agent),
        )
        ctx.agent.lifecycle.session_saved(sid)
        ctx.effect.info(f"Session auto-saved: {sid}")
    return ctx.effect.finish(control="exit", session_id=command.current_session_id)


def _handle_reset(command, ctx) -> CommandEffect:
    ctx.agent.reset()
    restore_config_runtime_defaults(ctx.config, ctx.agent)
    process_manager = getattr(ctx.agent, "process_manager", None)
    active_processes = (
        process_manager.active_count(
            owner_session_id=ctx.agent.current_session_id
        )
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


def _handle_compact(command, ctx) -> CommandEffect:
    before = estimate_tokens(ctx.agent.messages)

    if command.force_strategy == "":
        ctx.effect.warning(
            "Invalid compact strategy. Use: /compact force <snip|summarize|collapse>"
        )
        return ctx.effect.finish(control="continue")

    if command.force_strategy:
        force = getattr(ctx.agent, "force_compress_context", None)
        compressed = (
            force(command.force_strategy, ctx.agent.llm)
            if callable(force)
            else ctx.agent.context.force_compress(
                ctx.agent.messages,
                command.force_strategy,
                ctx.agent.llm,
            )
        )
        after = estimate_tokens(ctx.agent.messages)
        if compressed:
            ctx.effect.success(
                f"Forced {command.force_strategy}: {before} → {after} tokens ({len(ctx.agent.messages)} messages)"
            )
        else:
            ctx.effect.info(
                f"Forced {command.force_strategy}: no change ({before} tokens, {len(ctx.agent.messages)} messages)"
            )
        return ctx.effect.finish(control="continue")

    maybe_compress = getattr(ctx.agent, "maybe_compress_context", None)
    compressed = (
        maybe_compress(ctx.agent.llm, reason="manual compact command")
        if callable(maybe_compress)
        else ctx.agent.context.maybe_compress(ctx.agent.messages, ctx.agent.llm)
    )
    after = estimate_tokens(ctx.agent.messages)
    if compressed:
        ctx.effect.success(
            f"Compressed: {before} → {after} tokens ({len(ctx.agent.messages)} messages)"
        )
    else:
        ctx.effect.info(
            f"Nothing to compress ({before} tokens, {len(ctx.agent.messages)} messages)"
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
        snip_wall=thresholds["snip_wall"],
        semantic_wall=thresholds["semantic_wall"],
        snip_min_gain=thresholds["snip_min_gain"],
        rewrite_target=thresholds["rewrite_target"],
        emergency_at=thresholds["emergency_at"],
        cache_epoch=ctx.agent.context.cache_epoch,
    )

    ctx.effect.open_view(
        view.view_type,
        title="Token Usage",
        view_model=view,
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
        ctx.effect.info("Detailed LLM request/response traces disabled for this session.")
    return ctx.effect.finish(
        control="continue", state_changes={"llm_debug_trace": enabled}
    )


def _handle_config(command, ctx) -> CommandEffect:
    view = build_effective_config_view(ctx.config, ctx.agent)
    ctx.effect.open_view(
        view.view_type,
        title="Effective Configuration",
        view_model=view,
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
        "depth",
        "high_watermark",
        "coalesced",
        "transient_dropped",
        "must_deliver_waits",
        "must_deliver_timeouts",
        "closed_dropped",
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
        view.view_type,
        title="Runtime Performance",
        view_model=view,
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _format_percent(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "n/a"


def _build_usage_bar(current: int, maximum: int, width: int = 24) -> str:
    if maximum <= 0:
        return "`[unknown]`"
    ratio = max(0.0, min(1.0, current / maximum))
    filled = int(ratio * width)
    bar = "█" * filled + "·" * (width - filled)
    return f"`[{bar}] {_format_percent(ratio * 100)}`"


def _build_tokens_markdown(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    lifetime_total: int,
    current_context_tokens: int,
    max_context_tokens: int,
    context_percent: float | None,
    message_count: int,
    snip_at: int | None,
    summarize_at: int | None,
    collapse_at: int | None,
    snip_hit_count: int,
    summarize_hit_count: int,
    snip_exhausted: bool,
    summarize_exhausted: bool,
    max_hits: int,
) -> str:
    usage_bar = _build_usage_bar(current_context_tokens, max_context_tokens)
    remaining_tokens = (
        max(max_context_tokens - current_context_tokens, 0)
        if max_context_tokens
        else None
    )

    lines = [
        "**Session usage (provider-reported):**",
        f"- prompt tokens: `{prompt_tokens}`",
        f"- completion tokens: `{completion_tokens}`",
        f"- lifetime total: `{lifetime_total}`",
        "- note: these are cumulative usage stats reported by the model provider.",
        "",
        "**Current context window (local estimate):**",
        f"- estimated current context: `{current_context_tokens}` tokens",
        f"- max context: `{max_context_tokens}` tokens",
        f"- remaining before hard limit: `{remaining_tokens if remaining_tokens is not None else 'n/a'}` tokens",
        f"- usage: `{_format_percent(context_percent)}`",
        f"- visual: {usage_bar}",
        f"- messages currently in context: `{message_count}`",
        "- note: current context is estimated locally from persisted messages and runtime prompt pieces.",
    ]

    thresholds = []
    if snip_at is not None:
        threshold_pct = (
            round((snip_at / max_context_tokens) * 100, 1)
            if max_context_tokens
            else None
        )
        thresholds.append(
            f"- layer 1 / snip tool outputs: `{snip_at}` tokens ({_format_percent(threshold_pct)})"
        )
    if summarize_at is not None:
        threshold_pct = (
            round((summarize_at / max_context_tokens) * 100, 1)
            if max_context_tokens
            else None
        )
        thresholds.append(
            f"- layer 2 / summarize old turns: `{summarize_at}` tokens ({_format_percent(threshold_pct)})"
        )
    if collapse_at is not None:
        threshold_pct = (
            round((collapse_at / max_context_tokens) * 100, 1)
            if max_context_tokens
            else None
        )
        thresholds.append(
            f"- layer 3 / hard collapse: `{collapse_at}` tokens ({_format_percent(threshold_pct)})"
        )
    if thresholds:
        lines.append("")
        lines.append("**Compression thresholds:**")
        lines.extend(thresholds)

    lines.append("")
    lines.append("**Compression wall-hit state:**")
    snip_status = "exhausted" if snip_exhausted else f"{snip_hit_count}/{max_hits} hits"
    summarize_status = (
        "exhausted" if summarize_exhausted else f"{summarize_hit_count}/{max_hits} hits"
    )
    lines.append(f"- layer 1 (snip): `{snip_status}`")
    lines.append(f"- layer 2 (summarize): `{summarize_status}`")
    lines.append(
        "- meaning: a layer is marked `exhausted` after repeated attempts stop producing enough reduction."
    )

    return "\n".join(lines)


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="system.help",
                feature_id="system",
                description="Show command help and scope annotations",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/help"),),
                parser=_parse_help,
                handler=_handle_show_help,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="system.exit",
                feature_id="system",
                description="Exit the interface after auto-saving the current session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/quit"),),
                parser=_parse_exit,
                handler=_handle_exit,
            ),
            ActionSpec(
                action_id="system.reset",
                feature_id="system",
                description="[session] Reset in-memory conversation and session runtime overrides",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/reset"),),
                parser=_parse_reset,
                handler=_handle_reset,
            ),
            ActionSpec(
                action_id="system.compact",
                feature_id="system",
                description="[session] Compact the current conversation context",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/compact"),),
                parser=_parse_compact,
                handler=_handle_compact,
            ),
            ActionSpec(
                action_id="system.tokens",
                feature_id="system",
                description="[session] Show token usage for the current session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/tokens"),),
                parser=_parse_tokens,
                handler=_handle_tokens,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="system.debug",
                feature_id="system",
                description="[session] Toggle detailed LLM request/response traces",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
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
                feature_id="system",
                description="[session] Show recent runtime performance timings",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
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
                feature_id="system",
                description="Show effective configuration values, sources and diagnostics",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/config"),),
                parser=_parse_config,
                handler=_handle_config,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
        ]
    )
