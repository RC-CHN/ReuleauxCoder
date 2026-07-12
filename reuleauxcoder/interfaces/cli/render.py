"""CLI rendering - event-driven UI renderer."""

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape as _escape_markup
from rich.panel import Panel
from rich.text import Text

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
from reuleauxcoder.presentation import (
    PresentationPolicy,
    PresentationReducer,
    ReasoningDisplay,
    Verbosity,
)
from reuleauxcoder.presentation.policy import fold_text
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
        terminal_width_provider: Callable[[], int | None] | None = None,
    ):
        self.console = console_override or console
        self._active_content_block: _ContentBlock | None = None
        self.reducer = reducer or PresentationReducer(policy=policy)
        self.policy = self.reducer.policy
        self.view_registry = view_registry or create_cli_view_registry()
        self._terminal_width_provider = terminal_width_provider
        # Reasoning streaming state
        self._reasoning_label_printed: bool = False

    def close(self) -> None:
        """Release terminal handlers/resources held by the renderer."""
        self._active_content_block = None
        self.reducer.state.transcript.clear()
        self.reducer.state.seen_event_ids.clear()
        self.reducer.state.session_generations.clear()
        self.reducer.state.active_assistant_cells.clear()

    def on_runtime_event(self, event: RuntimeEvent) -> None:
        """Render one typed runtime event after reducing shared state."""
        self._refresh_terminal_width()
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
            if self.policy.verbosity is not Verbosity.COMPACT:
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
        elif self.policy.verbosity is not Verbosity.COMPACT:
            self.console.print(payload.message)

    def on_ui_event(self, event: UIEvent) -> None:
        """Handle a UI bus event."""
        self._refresh_terminal_width()
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
            render_interaction_request(
                self.console,
                event.payload.request,
                max_preview_lines=self.policy.tool_preview_lines,
                max_preview_chars=self.policy.tool_preview_chars,
            )
            return

        self._render_notification(event)

    def _refresh_terminal_width(self) -> None:
        if self._terminal_width_provider is None:
            return
        width = self._terminal_width_provider()
        if isinstance(width, int) and 20 <= width <= 500 and width != self.console.width:
            self.console.width = width

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
        if self.policy.reasoning_display is ReasoningDisplay.HIDDEN:
            return
        mode = display_mode or (
            "inline"
            if self.policy.reasoning_display is ReasoningDisplay.INLINE
            else "quiet"
        )

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
        args_str = brief(args, maxlen=max(24, self.console.width - 20)) if (
            args and self.policy.show_tool_args
        ) else ""
        call_text = f"{name}({args_str})" if args_str else f"{name}()"
        self.console.print(f"[cyan]›[/cyan] [bold]{call_text}[/bold]")

    def _render_tool_end(self, name: str, outcome: ToolOutcome) -> None:
        """Render tool call result."""
        display = self.policy.tool_preview(outcome)
        if not display:
            return
        if outcome.success:
            self.console.print(
                f"  [dim]{_escape_markup(display)}[/dim]", soft_wrap=True
            )
            diff = self.policy.tool_diff_preview(outcome)
            if diff:
                render_diff_panel(diff, self.console)
        else:
            self.console.print(
                f"  [red]× {name}: {_escape_markup(display)}[/red]",
                soft_wrap=True,
            )

    def _render_subagent_completed(self, payload: SubagentFinished) -> None:
        """Render a concise sub-agent completion notification."""
        body = f"id={payload.job_id} mode={payload.mode}"
        if payload.error:
            error = fold_text(
                payload.error,
                max_lines=self.policy.tool_preview_lines,
                max_chars=self.policy.tool_preview_chars,
            )
            self.console.print(
                f"[red]× subagent[/red] {body} {payload.status}: "
                f"{_escape_markup(error)}",
                soft_wrap=True,
            )
        else:
            self.console.print(f"[magenta]↳ subagent[/magenta] {body} {payload.status}")

    def _render_diff(self, result: str) -> None:
        """Render a diff with syntax highlighting."""
        render_diff_panel(
            result,
            self.console,
            max_lines=self.policy.tool_preview_lines,
            max_chars=self.policy.tool_preview_chars,
        )

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
        message = fold_text(
            event.message,
            max_lines=self.policy.tool_preview_lines,
            max_chars=self.policy.tool_preview_chars,
        )
        # Reasoning content display (/thinking command)
        if isinstance(event.payload, ReasoningNoticePayload):
            self._close_active_content_block()
            self.console.print(
                f"[bold bright_black]{_escape_markup(event.payload.title)}[/bold bright_black]"
            )
            self.console.print(message, soft_wrap=True)
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
        if not self.policy.should_render_notification(event.level.value):
            return
        if event.level is UIEventLevel.INFO:
            self.console.print(f"[dim]{_escape_markup(message)}[/dim]", soft_wrap=True)
            return
        if event.level is UIEventLevel.SUCCESS:
            self.console.print(
                f"[green]✓[/green] {_escape_markup(message)}", soft_wrap=True
            )
            return
        marker = "⚠" if event.level is UIEventLevel.WARNING else "×"
        category = (
            f"{event.kind.value}: " if event.kind is not UIEventKind.SYSTEM else ""
        )
        self.console.print(
            f"[{border_style}]{marker} {category}{_escape_markup(message)}"
            f"[/{border_style}]",
            soft_wrap=True,
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


def show_banner(
    model: str,
    base_url: str | None,
    version: str,
    *,
    console_override: Console | None = None,
    startup_events: Sequence[UIEvent] = (),
) -> None:
    from reuleauxcoder.infrastructure.platform import get_platform_info

    target = console_override or console
    platform_info = get_platform_info()
    shell = platform_info.get_preferred_shell()
    panel_width = min(88, target.width)
    value_width = max(8, panel_width - 16)
    body = Text()
    body.append(">_ ", style="dim")
    body.append("ReuleauxCoder", style="bold magenta")
    body.append(f" (v{version})", style="dim")
    body.append("\n\n")
    body.append("model:     ", style="dim")
    body.append(_truncate_middle(model, value_width))
    body.append("\n")
    body.append("directory: ", style="dim")
    body.append(_truncate_middle(str(Path.cwd()), value_width))
    body.append("\n")
    body.append("runtime:   ", style="dim")
    body.append(f"{platform_info.system.upper()} · {shell.value}")
    if base_url:
        body.append("\n")
        body.append("base:      ", style="dim")
        body.append(_truncate_middle(base_url, value_width), style="dim")

    visible_startup = [
        event for event in startup_events if event.level is not UIEventLevel.DEBUG
    ]
    if visible_startup:
        body.append("\n\n")
    markers = {
        UIEventLevel.INFO: ("• ", "dim"),
        UIEventLevel.SUCCESS: ("✓ ", "green"),
        UIEventLevel.WARNING: ("⚠ ", "yellow"),
        UIEventLevel.ERROR: ("× ", "red"),
        UIEventLevel.DEBUG: ("· ", "dim"),
    }
    for event_index, event in enumerate(visible_startup):
        marker, marker_style = markers[event.level]
        lines = event.message.splitlines() or [""]
        for line_index, line in enumerate(lines):
            body.append(marker if line_index == 0 else "  ", style=marker_style)
            body.append(line)
            if line_index < len(lines) - 1:
                body.append("\n")
        if event_index < len(visible_startup) - 1:
            body.append("\n")

    target.print(
        Panel(
            body,
            border_style="bright_black",
            width=panel_width,
            expand=False,
            padding=(0, 1),
        )
    )
    target.print(
        "  [cyan]/help[/cyan] commands  ·  [cyan]Ctrl+C[/cyan] cancel  ·  "
        "[cyan]/quit[/cyan] exit"
    )


def _truncate_middle(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    left = (width - 1 + 1) // 2
    right = width - 1 - left
    return f"{value[:left]}…{value[-right:]}" if right else f"{value[:left]}…"


def show_error(text: str) -> None:
    console.print(f"[red]{text}[/red]")


def show_warning(text: str) -> None:
    console.print(f"[yellow]{text}[/yellow]")


def show_info(text: str) -> None:
    console.print(text)
