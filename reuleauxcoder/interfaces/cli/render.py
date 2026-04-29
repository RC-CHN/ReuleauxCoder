"""CLI rendering - event-driven UI renderer."""

from dataclasses import dataclass, field
from typing import Literal

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.interfaces.cli.views.registry import create_cli_view_registry
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind, UIEventLevel
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry

console = Console()


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


@dataclass
class _ToolCallBlock:
    name: str
    args: dict | None
    result: str | None = None
    success: bool = True


@dataclass
class _NotificationBlock:
    level: UIEventLevel
    kind: UIEventKind
    message: str


class CLIRenderer:
    """Event-driven CLI renderer - subscribes to agent events."""

    def __init__(
        self,
        view_registry: ViewRendererRegistry | None = None,
        *,
        console_override: Console | None = None,
    ):
        self.console = console_override or console
        self._active_content_block: _ContentBlock | None = None
        self._completed_blocks: list[
            _ContentBlock | _ToolCallBlock | _NotificationBlock
        ] = []
        self.view_registry = view_registry or create_cli_view_registry()

    def close(self) -> None:
        """Release terminal handlers/resources held by the renderer."""
        self._active_content_block = None
        self._completed_blocks.clear()

    def on_event(self, event: AgentEvent) -> None:
        """Handle an agent event."""
        if event.event_type == AgentEventType.STREAM_TOKEN:
            self._render_token(event.data["token"])
        elif event.event_type == AgentEventType.TOOL_CALL_START:
            self._render_tool_start(event.tool_name, event.tool_args)
        elif event.event_type == AgentEventType.TOOL_CALL_END:
            self._render_tool_end(
                event.tool_name,
                event.tool_result,
                success=event.tool_success if event.tool_success is not None else True,
            )
        elif event.event_type == AgentEventType.SUBAGENT_COMPLETED:
            self._render_subagent_completed(event.data)
        elif event.event_type == AgentEventType.CHAT_END:
            self.finalize_response(
                event.data.get("response", ""),
                render_response=event.data.get("render_response", True),
            )
        elif event.event_type == AgentEventType.ERROR:
            self._render_error(event.error_message)

    def on_ui_event(self, event: UIEvent) -> None:
        """Handle a UI bus event."""
        if event.kind == UIEventKind.AGENT:
            agent_event = event.data.get("agent_event")
            if isinstance(agent_event, AgentEvent):
                self.on_event(agent_event)
            return

        if event.kind == UIEventKind.REMOTE and event.data.get("remote_stream"):
            self._render_remote_stream(event)
            return

        if event.kind == UIEventKind.VIEW:
            if self._render_view_event(event):
                return

        self._render_notification(event)

    def _render_token(self, token: str) -> None:
        """Append streamed content and flush complete markdown paragraphs."""
        if self._active_content_block is None:
            self._active_content_block = _ContentBlock()
        self._active_content_block.append(token)
        self._flush_completed_paragraphs()

    def _flush_completed_paragraphs(self) -> None:
        """Render completed paragraphs from the active content block."""
        block = self._active_content_block
        if block is None:
            return

        pending = block.pending_text
        cutoff = pending.rfind("\n\n")
        if cutoff < 0:
            return

        flush_text = pending[: cutoff + 2]
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
        self._completed_blocks.append(block)
        self._active_content_block = None

    def _render_tool_start(self, name: str, args: dict | None) -> None:
        """Render tool call start."""
        self._close_active_content_block()
        self._completed_blocks.append(_ToolCallBlock(name=name, args=args))
        args_str = brief(args) if args else ""
        call_text = f"{name}({args_str})" if args_str else f"{name}()"
        self.console.print(
            Panel(
                f"[bold cyan]{call_text}[/bold cyan]",
                title="TOOL CALL",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def _render_tool_end(
        self, name: str, result: str | None, success: bool = True
    ) -> None:
        """Render tool call result."""
        self._completed_blocks.append(
            _ToolCallBlock(name=name, args=None, result=result, success=success)
        )
        if not result:
            return
        # Special rendering for edit_file with diff
        if name == "edit_file" and "---" in result:
            self._render_diff(result)
        else:
            display = self._compact_tool_output(name, result)
            if success:
                self.console.print(f"[dim]{display}[/dim]")
            else:
                self.console.print(
                    Panel(
                        f"[red]{display}[/red]",
                        title=f"TOOL ERROR · {name}",
                        border_style="red",
                        box=box.ROUNDED,
                        padding=(0, 1),
                    )
                )

    def _render_subagent_completed(self, data: dict) -> None:
        """Render a concise sub-agent completion notification."""
        job_id = data.get("job_id", "?")
        mode = data.get("mode", "?")
        status = data.get("status", "?")
        title = f"SUBAGENT · {status.upper()}"
        body = f"id={job_id} mode={mode}"
        self.console.print(
            Panel(
                body,
                title=title,
                border_style="magenta",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def _compact_tool_output(self, tool_name: str, result: str) -> str:
        """Compact noisy tool output for terminal readability.

        When the result has already been truncated by
        ``ToolOutputTruncationHook`` (detected via the ``[truncated]``
        prefix), we preserve the header / footer lines containing the
        real stats and archive path, and show only the first and last
        3 lines of the wrapped content body.
        """
        # ── Already wrapped by ToolOutputTruncationHook ──
        if result.startswith("[truncated]"):
            lines = result.splitlines()
            try:
                begin = next(
                    i for i, ln in enumerate(lines)
                    if ln.startswith("--- BEGIN TRUNCATED OUTPUT ---")
                )
                end = next(
                    i for i, ln in enumerate(lines)
                    if ln.startswith("--- END TRUNCATED OUTPUT ---")
                )
            except StopIteration:
                return result  # malformed — show as-is

            header = lines[: begin + 1]   # stats + BEGIN marker
            body = lines[begin + 1 : end]
            footer = lines[end:]          # END marker

            if len(body) <= 6:
                return result              # small enough, no need to compact

            return (
                "\n".join(header) + "\n"
                + "\n".join(body[:3]) + "\n"
                + f"... ({len(body) - 6} more lines) ...\n"
                + "\n".join(body[-3:]) + "\n"
                + "\n".join(footer)
            )

        # ── Not pre-truncated — apply simple length cap ──
        text = result[:1200] + "..." if len(result) > 1200 else result
        lines = text.splitlines()
        if not lines:
            return text

        # read_file output is usually the noisiest; collapse more aggressively
        max_lines = 5 if tool_name == "read_file" else 20
        if len(lines) <= max_lines:
            return text

        kept = "\n".join(lines[:max_lines])
        omitted = len(lines) - max_lines
        return f"{kept}\n... ({omitted} more lines hidden)"

    def _render_diff(self, result: str) -> None:
        """Render a diff with syntax highlighting."""
        render_diff_panel(result, self.console)

    def _render_error(self, message: str | None) -> None:
        """Render an error message."""
        if message:
            self.console.print(f"[red]{message}[/red]")

    def _render_remote_stream(self, event: UIEvent) -> None:
        """Render raw remote stream chunk directly to terminal."""
        chunk = event.data.get("chunk")
        if not isinstance(chunk, str) or not chunk:
            return
        self._close_active_content_block()
        self.render_plain_text(chunk)

    def _render_notification(self, event: UIEvent) -> None:
        """Render a generic UI notification event."""
        border_style = {
            UIEventLevel.INFO: "blue",
            UIEventLevel.SUCCESS: "green",
            UIEventLevel.WARNING: "yellow",
            UIEventLevel.ERROR: "red",
            UIEventLevel.DEBUG: "bright_black",
        }[event.level]

        self._close_active_content_block()
        self._completed_blocks.append(
            _NotificationBlock(
                level=event.level, kind=event.kind, message=event.message
            )
        )

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

    def _render_view_event(self, event: UIEvent) -> bool:
        """Render known structured view events in the CLI."""
        view_type = event.data.get("view_type")
        if not isinstance(view_type, str) or not view_type:
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
            self._completed_blocks.append(block)
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
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")


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


def render_diff_panel(result: str, target_console: Console | None = None) -> None:
    """Render a diff with syntax highlighting into the given console."""
    out = target_console or console
    try:
        syntax = Syntax(result, "diff", theme="monokai", line_numbers=False)
        out.print(Panel(syntax, border_style="green", padding=(0, 1)))
    except Exception:
        out.print(f"[dim]{result[:500]}[/dim]")
