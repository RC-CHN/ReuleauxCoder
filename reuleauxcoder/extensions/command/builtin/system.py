"""Builtin system command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.help import build_help_markdown
from reuleauxcoder.app.commands.matchers import match_template, matches_any
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    EmptyCommand,
    TEXT_REQUIRED,
    UI_TARGETS,
    enum_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.views.common import render_markdown_panel
from reuleauxcoder.interfaces.view_registration import register_view

_FORCE_COMPACT_STRATEGIES = {"snip", "summarize", "collapse"}


@dataclass(frozen=True, slots=True)
class ExitCommand:
    current_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompactContextCommand:
    force_strategy: str | None = None


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

    captures = match_template(user_input, "/compact force {strategy}", case_insensitive=True)
    if captures is None:
        return None

    try:
        strategy = enum_text(_FORCE_COMPACT_STRATEGIES, case_insensitive=True).parse(captures["strategy"])
    except ParamParseError:
        return CompactContextCommand(force_strategy="")

    return CompactContextCommand(force_strategy=strategy)


def _parse_tokens(user_input: str, parse_ctx):
    if match_template(user_input, "/tokens") is not None:
        return EmptyCommand()
    return None


@register_view(view_type="help", ui_targets={"cli"})
def render_help_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    markdown = payload.get("markdown")
    return isinstance(markdown, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown,
        title="Help",
    )


@register_view(view_type="token_usage", ui_targets={"cli"})
def render_token_usage_view(renderer, event) -> bool:
    payload = event.data.get("payload") or {}
    markdown = payload.get("markdown")
    return isinstance(markdown, str) and render_markdown_panel(
        renderer,
        markdown_text=markdown,
        title="Token Usage",
    )


def _handle_show_help(command, ctx) -> CommandResult:
    if ctx.ui_profile is None:
        markdown = "No active UI profile; help unavailable."
    elif ctx.action_registry is None:
        markdown = "No action registry available; help unavailable."
    else:
        markdown = build_help_markdown(ctx.ui_profile, ctx.action_registry)
    payload = {"markdown": markdown}
    ctx.ui_bus.open_view("help", title="ReuleauxCoder Help", payload=payload, reuse_key="help")
    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(view_type="help", title="ReuleauxCoder Help", payload=payload, reuse_key="help")
        ],
        payload=payload,
    )


def _handle_exit(command, ctx) -> CommandResult:
    if ctx.agent.messages:
        sid = SessionStore(ctx.sessions_dir).save(
            ctx.agent.messages,
            ctx.config.model,
            command.current_session_id,
            is_exit=True,
            total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
            total_completion_tokens=ctx.agent.state.total_completion_tokens,
            active_mode=getattr(ctx.agent, "active_mode", None),
        )
        ctx.ui_bus.info(f"Session auto-saved: {sid}")
    return CommandResult(action="exit", session_id=command.current_session_id)


def _handle_reset(command, ctx) -> CommandResult:
    ctx.agent.reset()
    ctx.ui_bus.warning("Conversation reset (in-memory only, does not delete saved sessions).")
    return CommandResult(action="continue")


def _handle_compact(command, ctx) -> CommandResult:
    before = estimate_tokens(ctx.agent.messages)

    if command.force_strategy == "":
        ctx.ui_bus.warning("Invalid compact strategy. Use: /compact force <snip|summarize|collapse>")
        return CommandResult(action="continue")

    if command.force_strategy:
        compressed = ctx.agent.context.force_compress(
            ctx.agent.messages,
            command.force_strategy,
            ctx.agent.llm,
        )
        after = estimate_tokens(ctx.agent.messages)
        if compressed:
            ctx.ui_bus.success(
                f"Forced {command.force_strategy}: {before} → {after} tokens ({len(ctx.agent.messages)} messages)"
            )
        else:
            ctx.ui_bus.info(
                f"Forced {command.force_strategy}: no change ({before} tokens, {len(ctx.agent.messages)} messages)"
            )
        return CommandResult(action="continue")

    compressed = ctx.agent.context.maybe_compress(ctx.agent.messages, ctx.agent.llm)
    after = estimate_tokens(ctx.agent.messages)
    if compressed:
        ctx.ui_bus.success(f"Compressed: {before} → {after} tokens ({len(ctx.agent.messages)} messages)")
    else:
        ctx.ui_bus.info(f"Nothing to compress ({before} tokens, {len(ctx.agent.messages)} messages)")
    return CommandResult(action="continue")


def _handle_tokens(command, ctx) -> CommandResult:
    prompt_tokens = ctx.agent.state.total_prompt_tokens
    completion_tokens = ctx.agent.state.total_completion_tokens
    lifetime_total = prompt_tokens + completion_tokens

    # Current context is always estimated locally from persisted/runtime prompt pieces.
    current_context_tokens = ctx.agent.context.get_context_tokens(ctx.agent.messages)
    max_context_tokens = getattr(ctx.agent.context, "max_tokens", None) or getattr(
        ctx.config, "max_context_tokens", 0
    )
    if max_context_tokens:
        context_ratio = current_context_tokens / max_context_tokens
        context_percent = round(context_ratio * 100, 1)
    else:
        context_percent = None

    # Compression wall-hit state
    snip_hit_count = getattr(ctx.agent.context, "_snip_hit_count", 0)
    summarize_hit_count = getattr(ctx.agent.context, "_summarize_hit_count", 0)
    snip_exhausted = getattr(ctx.agent.context, "_snip_exhausted", False)
    summarize_exhausted = getattr(ctx.agent.context, "_summarize_exhausted", False)
    max_hits = getattr(ctx.agent.context, "_max_hits", 3)

    payload = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "lifetime_total": lifetime_total,
        "current_context_tokens": current_context_tokens,
        "max_context_tokens": max_context_tokens,
        "context_percent": context_percent,
        "message_count": len(ctx.agent.messages),
        "snip_at": getattr(ctx.agent.context, "_snip_at", None),
        "summarize_at": getattr(ctx.agent.context, "_summarize_at", None),
        "collapse_at": getattr(ctx.agent.context, "_collapse_at", None),
        "snip_hit_count": snip_hit_count,
        "summarize_hit_count": summarize_hit_count,
        "snip_exhausted": snip_exhausted,
        "summarize_exhausted": summarize_exhausted,
        "max_hits": max_hits,
        "markdown": _build_tokens_markdown(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            lifetime_total=lifetime_total,
            current_context_tokens=current_context_tokens,
            max_context_tokens=max_context_tokens,
            context_percent=context_percent,
            message_count=len(ctx.agent.messages),
            snip_at=getattr(ctx.agent.context, "_snip_at", None),
            summarize_at=getattr(ctx.agent.context, "_summarize_at", None),
            collapse_at=getattr(ctx.agent.context, "_collapse_at", None),
            snip_hit_count=snip_hit_count,
            summarize_hit_count=summarize_hit_count,
            snip_exhausted=snip_exhausted,
            summarize_exhausted=summarize_exhausted,
            max_hits=max_hits,
        ),
    }

    ctx.ui_bus.open_view(
        "token_usage",
        title="Token Usage",
        payload=payload,
        reuse_key="token_usage",
    )

    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(
                view_type="token_usage",
                title="Token Usage",
                payload=payload,
                reuse_key="token_usage",
            )
        ],
        payload=payload,
    )


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
    remaining_tokens = max(max_context_tokens - current_context_tokens, 0) if max_context_tokens else None

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
        threshold_pct = round((snip_at / max_context_tokens) * 100, 1) if max_context_tokens else None
        thresholds.append(f"- layer 1 / snip tool outputs: `{snip_at}` tokens ({_format_percent(threshold_pct)})")
    if summarize_at is not None:
        threshold_pct = round((summarize_at / max_context_tokens) * 100, 1) if max_context_tokens else None
        thresholds.append(f"- layer 2 / summarize old turns: `{summarize_at}` tokens ({_format_percent(threshold_pct)})")
    if collapse_at is not None:
        threshold_pct = round((collapse_at / max_context_tokens) * 100, 1) if max_context_tokens else None
        thresholds.append(f"- layer 3 / hard collapse: `{collapse_at}` tokens ({_format_percent(threshold_pct)})")
    if thresholds:
        lines.append("")
        lines.append("**Compression thresholds:**")
        lines.extend(thresholds)

    lines.append("")
    lines.append("**Compression wall-hit state:**")
    snip_status = "exhausted" if snip_exhausted else f"{snip_hit_count}/{max_hits} hits"
    summarize_status = "exhausted" if summarize_exhausted else f"{summarize_hit_count}/{max_hits} hits"
    lines.append(f"- layer 1 (snip): `{snip_status}`")
    lines.append(f"- layer 2 (summarize): `{summarize_status}`")
    lines.append("- meaning: a layer is marked `exhausted` after repeated attempts stop producing enough reduction.")

    return "\n".join(lines)


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="system.help",
                feature_id="system",
                description="Show command help",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/help"),),
                parser=_parse_help,
                handler=_handle_show_help,
            ),
            ActionSpec(
                action_id="system.exit",
                feature_id="system",
                description="Exit interface",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/quit"),),
                parser=_parse_exit,
                handler=_handle_exit,
            ),
            ActionSpec(
                action_id="system.reset",
                feature_id="system",
                description="Reset in-memory conversation",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/reset"),),
                parser=_parse_reset,
                handler=_handle_reset,
            ),
            ActionSpec(
                action_id="system.compact",
                feature_id="system",
                description="Compact conversation context",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/compact"),),
                parser=_parse_compact,
                handler=_handle_compact,
            ),
            ActionSpec(
                action_id="system.tokens",
                feature_id="system",
                description="Show token usage",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/tokens"),),
                parser=_parse_tokens,
                handler=_handle_tokens,
            ),
        ]
    )
