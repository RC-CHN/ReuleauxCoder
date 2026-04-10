"""Shared handlers for general runtime/status commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import CommandContext, CommandResult, OpenViewRequest, ShowTokensCommand
from reuleauxcoder.domain.context.manager import estimate_tokens


def handle_show_tokens(command: ShowTokensCommand, ctx: CommandContext) -> CommandResult:
    """Show token usage summary for the session and current context window."""
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
