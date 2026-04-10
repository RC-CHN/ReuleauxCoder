"""Shared handlers for general runtime/status commands."""

from __future__ import annotations

from reuleauxcoder.app.commands.models import (
    CommandContext,
    CommandResult,
    CompactContextCommand,
    ExitCommand,
    OpenViewRequest,
    ResetConversationCommand,
    ShowHelpCommand,
    ShowTokensCommand,
)
from reuleauxcoder.domain.context.manager import estimate_tokens
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore


def handle_show_help(command: ShowHelpCommand, ctx: CommandContext) -> CommandResult:
    """Show help in a structured view."""
    payload = {"markdown": _build_help_markdown()}
    ctx.ui_bus.open_view("help", title="ReuleauxCoder Help", payload=payload, reuse_key="help")
    return CommandResult(
        action="continue",
        view_requests=[
            OpenViewRequest(view_type="help", title="ReuleauxCoder Help", payload=payload, reuse_key="help")
        ],
        payload=payload,
    )


def handle_exit(command: ExitCommand, ctx: CommandContext) -> CommandResult:
    """Exit the interface, auto-saving current conversation if needed."""
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


def handle_reset_conversation(command: ResetConversationCommand, ctx: CommandContext) -> CommandResult:
    """Clear current in-memory conversation only."""
    ctx.agent.reset()
    ctx.ui_bus.warning("Conversation reset (in-memory only, does not delete saved sessions).")
    return CommandResult(action="continue")



def handle_compact_context(command: CompactContextCommand, ctx: CommandContext) -> CommandResult:
    """Compress current conversation context."""
    before = estimate_tokens(ctx.agent.messages)
    compressed = ctx.agent.context.maybe_compress(ctx.agent.messages, ctx.agent.llm)
    after = estimate_tokens(ctx.agent.messages)
    if compressed:
        ctx.ui_bus.success(f"Compressed: {before} → {after} tokens ({len(ctx.agent.messages)} messages)")
    else:
        ctx.ui_bus.info(f"Nothing to compress ({before} tokens, {len(ctx.agent.messages)} messages)")
    return CommandResult(action="continue")


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


def _build_help_markdown() -> str:
    return "\n".join(
        [
            "**Commands:**",
            "- `/help` — Show this help",
            "- `/reset` — Clear current in-memory conversation only",
            "- `/new` — Start a new conversation (auto-save previous)",
            "- `/model` — List model profiles and current active profile",
            "- `/model <profile>` — Switch to a configured model profile",
            "- `/tokens` — Show token usage",
            "- `/compact` — Compress conversation context",
            "- `/save` — Save session to disk",
            "- `/sessions` — List saved sessions",
            "- `/session <id>` — Resume a saved session in current process",
            "- `/session latest` — Resume latest saved session",
            "- `/approval show` — Show approval rules",
            "- `/approval set ...` — Update approval rules",
            "- `/mcp show` — Show MCP server status",
            "- `/mcp enable <s>` — Enable one MCP server",
            "- `/mcp disable <s>` — Disable one MCP server",
            "- `/quit` — Exit ReuleauxCoder",
        ]
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
