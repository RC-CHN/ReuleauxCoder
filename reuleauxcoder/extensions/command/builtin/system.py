"""Builtin system command extension registration and handlers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.help import build_help_markdown
from reuleauxcoder.app.commands.models import CommandResult, OpenViewRequest
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.extensions.command.builtin.common import EmptyCommand, TEXT_REQUIRED, UI_TARGETS, slash_trigger
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore

_FORCE_COMPACT_STRATEGIES = {"snip", "summarize", "collapse"}


@dataclass(frozen=True, slots=True)
class ExitCommand:
    current_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompactContextCommand:
    force_strategy: str | None = None


def _parse_help(user_input: str, parse_ctx):
    if user_input == "/help":
        return EmptyCommand()
    return None


def _parse_exit(user_input: str, parse_ctx):
    if user_input.lower() in {"/quit", "/exit"}:
        return ExitCommand(current_session_id=parse_ctx.current_session_id)
    return None


def _parse_reset(user_input: str, parse_ctx):
    if user_input == "/reset":
        return EmptyCommand()
    return None


def _parse_compact(user_input: str, parse_ctx):
    lowered = user_input.lower()
    if user_input == "/compact":
        return CompactContextCommand()
    if lowered.startswith("/compact force "):
        strategy = lowered[len("/compact force ") :].strip()
        if strategy in _FORCE_COMPACT_STRATEGIES:
            return CompactContextCommand(force_strategy=strategy)
        return CompactContextCommand(force_strategy="")
    return None


def _parse_tokens(user_input: str, parse_ctx):
    if user_input == "/tokens":
        return EmptyCommand()
    return None


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

    current_context_tokens = estimate_tokens(ctx.agent.messages)
    max_context_tokens = getattr(ctx.agent.context, "max_tokens", None) or getattr(
        ctx.config, "max_context_tokens", 0
    )
    if max_context_tokens:
        context_ratio = current_context_tokens / max_context_tokens
        context_percent = round(context_ratio * 100, 1)
    else:
        context_percent = None

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
) -> str:
    lines = [
        "**Session usage:**",
        f"- prompt: `{prompt_tokens}`",
        f"- completion: `{completion_tokens}`",
        f"- total: `{lifetime_total}`",
        "",
        "**Current context window:**",
        f"- estimated current context: `{current_context_tokens}` tokens",
        f"- max context: `{max_context_tokens}` tokens",
        f"- usage: `{context_percent if context_percent is not None else 'n/a'}%`",
        f"- messages in context: `{message_count}`",
    ]

    thresholds = []
    if snip_at is not None:
        thresholds.append(f"- snip tool outputs at: `{snip_at}`")
    if summarize_at is not None:
        thresholds.append(f"- summarize old turns at: `{summarize_at}`")
    if collapse_at is not None:
        thresholds.append(f"- hard collapse at: `{collapse_at}`")
    if thresholds:
        lines.append("")
        lines.append("**Compression thresholds:**")
        lines.extend(thresholds)

    return "\n".join(lines)


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
