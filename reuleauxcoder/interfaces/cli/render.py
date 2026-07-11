"""CLI rendering - event-driven UI renderer."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as _escape_markup
from rich.panel import Panel

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.tool_outcome import ToolOutcome
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    AssistantContentDelta,
    ChatCompleted,
    DiagnosticsCleared,
    DiagnosticsPublished,
    ErrorOccurred,
    NotificationRaised,
    ReasoningDelta,
    RuntimeEvent,
    StreamChunk,
    SubagentFinished,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutputDelta,
    TurnFinished,
    agent_event_to_runtime_event,
)
from reuleauxcoder.interfaces.cli.views.registry import create_cli_view_registry
from reuleauxcoder.interfaces.cli.terminal import render_diff_panel
from reuleauxcoder.interfaces.events import (
    ReasoningNoticePayload,
    InteractionPromptPayload,
    RemoteStreamPayload,
    RuntimeEventPayload,
    UIEvent,
    UIEventKind,
    UIEventLevel,
    ViewEventPayload,
)
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry
from reuleauxcoder.presentation import PresentationPolicy, PresentationReducer
from reuleauxcoder.interfaces.cli.interaction_presenter import (
    render_interaction_request,
)

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

console = Console()

# ------------------------------------------------------------------ markdown-it block-level token types that are self-closing
# (no separate open/close tokens).  Code fences ("fence") are the key
# one — treating them as atomic blocks prevents streaming code-block
# content from being split across render calls when a double-newline
# appears inside the fence.
_SELF_CLOSING_BLOCKS: frozenset[str] = frozenset(
    ("fence", "code_block", "hr", "html_block")
)

_parser: "MarkdownIt | None" = None


def _find_committed_boundary(text: str) -> int | None:
    """Return the character offset up to which *text* can be safely committed.

    Parses *text* into block-level tokens via ``markdown-it-py`` and
    confirms every block except the last one (which may be incomplete
    due to streaming truncation).  Returns ``None`` when there are
    fewer than 2 blocks (nothing confirmed yet).
    """
    global _parser
    if _parser is None:
        from markdown_it import MarkdownIt

        _parser = MarkdownIt().enable("strikethrough").enable("table")

    tokens = _parser.parse(text)

    # Collect only top-level block boundaries by tracking nesting depth.
    # Nested tokens (e.g. list_item_open inside bullet_list_open) are not
    # independent blocks — otherwise lists and blockquotes get split.
    block_maps: list[list[int]] = []
    depth = 0
    for t in tokens:
        if t.nesting == 1:
            if depth == 0 and t.map is not None:
                block_maps.append(t.map)
            depth += 1
        elif t.nesting == -1:
            depth -= 1
        elif depth == 0 and t.type in _SELF_CLOSING_BLOCKS and t.map is not None:
            block_maps.append(t.map)

    if len(block_maps) < 2:
        return None

    # Convert end-line number of the *second-to-last* block to a char offset.
    target_line = block_maps[-2][1]
    offset = 0
    for _ in range(target_line):
        offset = text.index("\n", offset) + 1
    return offset


@dataclass
class _ContentBlock:
    kind: Literal["text"] = "text"
    text_parts: list[str] = field(default_factory=list)
    rendered_length: int = 0

    def append(self, text: str) -> None:
        self.text_parts.append(text)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def pending_text(self) -> str:
        return self.text[self.rendered_length :]

    @property
    def is_empty(self) -> bool:
        return not self.text_parts


class CLIRenderer:
    """Event-driven CLI renderer - subscribes to agent events."""

    def __init__(
        self,
        view_registry: ViewRendererRegistry | None = None,
        *,
        console_override: Console | None = None,
        reducer: PresentationReducer | None = None,
        policy: PresentationPolicy | None = None,
    ):
        self.console = console_override or console
        self._active_content_block: _ContentBlock | None = None
        self.reducer = reducer or PresentationReducer(policy=policy)
        self.policy = self.reducer.policy
        self.view_registry = view_registry or create_cli_view_registry()
        # Reasoning streaming state
        self._reasoning_label_printed: bool = False

    def close(self) -> None:
        """Release terminal handlers/resources held by the renderer."""
        self._active_content_block = None
        self.reducer.state.transcript.clear()
        self.reducer.state.seen_event_ids.clear()
        self.reducer.state.session_generations.clear()
        self.reducer.state.active_assistant_cells.clear()

    def on_event(self, event: AgentEvent) -> None:
        """Compatibility entry point for callers still emitting AgentEvent."""
        self.on_runtime_event(agent_event_to_runtime_event(event))

    def on_runtime_event(self, event: RuntimeEvent) -> None:
        """Render one typed runtime event after reducing shared state."""
        was_seen = event.event_id in self.reducer.state.seen_event_ids
        changes = self.reducer.apply(event)
        if was_seen:
            return

        payload = event.payload
        if isinstance(payload, AssistantContentDelta):
            if changes:
                self._render_token(payload.text)
        elif isinstance(payload, ReasoningDelta):
            self._render_reasoning(payload.text, payload.display_mode)
        elif isinstance(payload, StreamChunk):
            if payload.reasoning:
                self._render_reasoning(payload.text, payload.display_mode)
            elif changes:
                self._render_token(payload.text)
        elif isinstance(payload, ToolCallStarted) and changes:
            self._render_tool_start(payload.tool_name, payload.arguments)
        elif isinstance(payload, ToolCallFinished) and changes:
            self._render_tool_end(payload.tool_name, payload.outcome)
        elif isinstance(payload, ToolOutputDelta) and changes:
            self.console.print(payload.text, end="", markup=False, highlight=False)
        elif isinstance(payload, SubagentFinished) and changes:
            self._render_subagent_completed(payload)
        elif isinstance(payload, (TurnFinished, ChatCompleted)):
            self.finalize_response(
                payload.response,
                render_response=payload.render_response,
            )
        elif isinstance(payload, ErrorOccurred):
            self._render_error(payload.message)
        elif isinstance(payload, NotificationRaised) and changes:
            self._render_runtime_notification(payload)
        elif isinstance(payload, DiagnosticsPublished) and changes:
            if payload.diagnostics:
                self.console.print(
                    f"LSP: {len(payload.diagnostics)} diagnostic(s) in "
                    f"{payload.file_path}"
                )
        elif isinstance(payload, DiagnosticsCleared) and changes:
            if self.policy.verbosity != "compact":
                self.console.print(f"LSP: clean {payload.file_path}")
        elif isinstance(payload, ApprovalRequested) and changes:
            self.console.print(f"Approval requested: {payload.title}")
        elif isinstance(payload, ApprovalResolved) and changes:
            status = "approved" if payload.approved else "denied"
            self.console.print(f"Approval {status}: {payload.request_id}")

    def _render_runtime_notification(self, payload: NotificationRaised) -> None:
        level = payload.severity.lower()
        if level == "error":
            self.console.print(f"[red]{payload.message}[/red]")
        elif level == "warning":
            self.console.print(f"[yellow]{payload.message}[/yellow]")
        elif self.policy.verbosity != "compact":
            self.console.print(payload.message)

    def on_ui_event(self, event: UIEvent) -> None:
        """Handle a UI bus event."""
        if event.kind == UIEventKind.AGENT:
            if isinstance(event.payload, RuntimeEventPayload):
                self.on_runtime_event(event.payload.event)
            return

        if isinstance(event.payload, RemoteStreamPayload):
            self._render_remote_stream(event.payload)
            return

        if isinstance(event.payload, ViewEventPayload):
            if self._render_view_event(event.payload, event):
                return

        if isinstance(event.payload, InteractionPromptPayload):
            self._close_active_content_block()
            render_interaction_request(self.console, event.payload.request)
            return

        self._render_notification(event)

    def _render_token(self, token: str) -> None:
        """Append streamed content and flush complete markdown paragraphs."""
        # Reset reasoning label state when content begins
        if self._reasoning_label_printed:
            self._reasoning_label_printed = False
        if self._active_content_block is None:
            self._active_content_block = _ContentBlock()
        self._active_content_block.append(token)
        self._flush_completed_paragraphs()

    def _render_reasoning(
        self, token: str, display_mode: str | None = None
    ) -> None:
        """Render a streamed reasoning token.

        In *quiet* mode (default): prints ``🤔 Thinking...`` once, then
        silently accumulates the rest.
        In *inline* mode: streams reasoning tokens in dim grey, raw text
        (no Markdown parsing).
        """
        mode = display_mode or "quiet"

        if mode == "quiet":
            if not self._reasoning_label_printed:
                self.console.print("  [dim]Thinking...[/dim]")
                self._reasoning_label_printed = True
            return

        # inline mode
        if not self._reasoning_label_printed:
            self._close_active_content_block()
            self.console.print("  [dim]Thinking: [/dim]", end="")
            self._reasoning_label_printed = True
        self.console.print(f"[dim]{_escape_markup(token)}[/dim]", end="")

    def _flush_completed_paragraphs(self) -> None:
        """Render completed blocks from the active content block.

        Uses markdown-it block-token parsing to find a safe commit
        boundary — all blocks except the last (potentially incomplete)
        one are rendered.  This prevents orphaned code fences (empty
        dark blocks) when a double-newline inside a fenced code block
        would otherwise split it across two render calls.
        """
        block = self._active_content_block
        if block is None:
            return

        pending = block.pending_text
        if not pending:
            return

        boundary = _find_committed_boundary(pending)
        if boundary is None:
            return

        flush_text = pending[:boundary]
        if flush_text:
            self.render_content_markdown(flush_text)
            block.rendered_length += len(flush_text)

    def _flush_remaining_content(self) -> None:
        """Render any remaining buffered content from the active block."""
        block = self._active_content_block
        if block is None:
            return

        pending = block.pending_text
        if pending:
            self.render_content_markdown(pending)
            block.rendered_length = len(block.text)

    def _close_active_content_block(self) -> None:
        """Finalize the active content block before structured output."""
        block = self._active_content_block
        if block is None:
            return
        self._flush_remaining_content()
        if not block.is_empty and not block.text.endswith("\n"):
            self.render_plain_text("\n")
        self._active_content_block = None

    def _render_tool_start(self, name: str, args: dict | None) -> None:
        """Render tool call start."""
        self._close_active_content_block()
        args_str = brief(args) if args else ""
        call_text = f"{name}({args_str})" if args_str else f"{name}()"
        self.console.print(f"[cyan]›[/cyan] [bold]{call_text}[/bold]")

    def _render_tool_end(self, name: str, outcome: ToolOutcome) -> None:
        """Render tool call result."""
        result = outcome.display_text
        if not result:
            return
        display = self.policy.tool_preview(outcome)
        if outcome.success:
            self.console.print(f"  [dim]{_escape_markup(display)}[/dim]")
        else:
            self.console.print(
                Panel(
                    f"[red]{_escape_markup(display)}[/red]",
                    title=f"TOOL ERROR · {name}",
                    border_style="red",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )

    def _render_subagent_completed(self, payload: SubagentFinished) -> None:
        """Render a concise sub-agent completion notification."""
        body = f"id={payload.job_id} mode={payload.mode}"
        if payload.error:
            self.console.print(
                Panel(
                    f"{body}\n{payload.error}",
                    title=f"SUBAGENT · {payload.status.upper()}",
                    border_style="red",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        else:
            self.console.print(f"[magenta]↳ subagent[/magenta] {body} {payload.status}")

    def _render_diff(self, result: str) -> None:
        """Render a diff with syntax highlighting."""
        render_diff_panel(result, self.console)

    def _render_error(self, message: str | None) -> None:
        """Render an error message."""
        if message:
            self.console.print(f"[red]{message}[/red]")

    def _render_remote_stream(self, payload: RemoteStreamPayload) -> None:
        """Render raw remote stream chunk directly to terminal."""
        if not payload.chunk:
            return
        self._close_active_content_block()
        self.render_plain_text(payload.chunk)

    def _render_notification(self, event: UIEvent) -> None:
        """Render a generic UI notification event."""
        # Reasoning content display (/thinking command)
        if isinstance(event.payload, ReasoningNoticePayload):
            self._close_active_content_block()
            self.console.print(
                Panel(
                    event.message,
                    title=event.payload.title,
                    border_style="bright_black",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
            return

        border_style = {
            UIEventLevel.INFO: "blue",
            UIEventLevel.SUCCESS: "green",
            UIEventLevel.WARNING: "yellow",
            UIEventLevel.ERROR: "red",
            UIEventLevel.DEBUG: "bright_black",
        }[event.level]

        self._close_active_content_block()
        self.reducer.append_notice(
            notice_id=f"{event.timestamp}:{event.kind.value}:{event.message}",
            message=event.message,
            level=event.level.value,
            category=event.kind.value,
        )
        if event.level is UIEventLevel.DEBUG and self.policy.verbosity.value != "debug":
            return
        if event.level is UIEventLevel.INFO:
            self.console.print(f"[dim]{_escape_markup(event.message)}[/dim]")
            return
        if event.level is UIEventLevel.SUCCESS:
            self.console.print(f"[green]✓[/green] {_escape_markup(event.message)}")
            return
        title = f"{event.kind.value.upper()} · {event.level.value.upper()}"
        self.console.print(
            Panel(
                event.message,
                title=title,
                border_style=border_style,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def _render_view_event(
        self, payload: ViewEventPayload, event: UIEvent
    ) -> bool:
        """Render known structured view events in the CLI."""
        view_type = payload.view_type
        if not view_type:
            return False

        spec = self.view_registry.get(view_type)
        if spec is None:
            self._render_notification(
                UIEvent.debug(
                    f"No CLI renderer registered for view_type '{view_type}'",
                    kind=UIEventKind.VIEW,
                    view_type=view_type,
                )
            )
            return False

        return spec.render(self, event)

    def finalize_response(self, response: str, *, render_response: bool = True) -> None:
        """Finalize response rendering for agent output."""
        if self._active_content_block is not None:
            self._close_active_content_block()
        elif response and render_response:
            block = _ContentBlock()
            block.append(response)
            block.rendered_length = len(response)
            self.render_content_markdown(response)

    def render_content_markdown(self, text: str) -> None:
        """Render assistant content as markdown, falling back to plain text."""
        try:
            self.console.print(Markdown(text), end="")
        except Exception:
            self.render_plain_text(text)

    def render_plain_text(self, text: str) -> None:
        """Render raw text without markdown parsing."""
        self.console.print(text, end="")

    def render_markdown(self, text: str) -> None:
        """Backward-compatible plain text output hook used by tests."""
        self.render_plain_text(text)


def brief(kwargs: dict, maxlen: int = 80) -> str:
    """Brief representation of kwargs for display."""
    if not kwargs:
        return ""
    parts: list[str] = []
    for key, value in kwargs.items():
        if isinstance(value, str) and len(repr(value)) > 42:
            value_text = repr(value[:36] + "…")
        else:
            value_text = repr(value)
            if len(value_text) > 42:
                value_text = value_text[:41] + "…"
        part = f"{key}={value_text}"
        candidate = ", ".join([*parts, part])
        if len(candidate) > maxlen:
            parts.append("…")
            break
        parts.append(part)
    return ", ".join(parts)


def show_banner(model: str, base_url: str | None, version: str) -> None:
    from reuleauxcoder.infrastructure.platform import get_platform_info

    platform_info = get_platform_info()
    shell = platform_info.get_preferred_shell()
    platform_line = f"Platform: [yellow]{platform_info.system.upper()}[/yellow]  Shell: [yellow]{shell.value}[/yellow]"

    console.print(
        Panel(
            f"[bold]ReuleauxCoder[/bold] v{version}\n"
            f"Model: [cyan]{model}[/cyan]"
            + (f"  Base: [dim]{base_url}[/dim]" if base_url else "")
            + f"\n{platform_line}"
            + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]/quit[/bold] to exit.",
            border_style="blue",
        )
    )


def show_error(text: str) -> None:
    console.print(f"[red]{text}[/red]")


def show_warning(text: str) -> None:
    console.print(f"[yellow]{text}[/yellow]")


def show_info(text: str) -> None:
    console.print(text)
