"""CLI rendering - event-driven UI renderer."""

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.interfaces.cli.views.registry import create_cli_view_registry
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind, UIEventLevel
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry

console = Console()


class CLIRenderer:
    """Event-driven CLI renderer - subscribes to agent events."""

    def __init__(self, view_registry: ViewRendererRegistry | None = None):
        self.console = console
        self._streamed_tokens: list[str] = []
        self._stream_has_output = False
        self.view_registry = view_registry or create_cli_view_registry()

    def close(self) -> None:
        """Release terminal handlers/resources held by the renderer."""
        self._streamed_tokens.clear()
        self._stream_has_output = False

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

        if event.kind == UIEventKind.VIEW:
            if self._render_view_event(event):
                return

        self._render_notification(event)

    def _render_token(self, token: str) -> None:
        """Render streamed tokens immediately as plain text."""
        self._streamed_tokens.append(token)
        self._stream_has_output = True
        self.render_markdown(token)

    def _finish_stream_line(self) -> None:
        """Ensure subsequent structured output starts on a fresh line."""
        if self._stream_has_output and self._streamed_tokens:
            if not self._streamed_tokens[-1].endswith("\n"):
                self.render_markdown("\n")
        self._streamed_tokens.clear()
        self._stream_has_output = False

    def _render_tool_start(self, name: str, args: dict | None) -> None:
        """Render tool call start."""
        if self._stream_has_output:
            self._finish_stream_line()
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

    def _render_tool_end(self, name: str, result: str | None, success: bool = True) -> None:
        """Render tool call result."""
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
        """Compact noisy tool output for terminal readability."""
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

    def _render_notification(self, event: UIEvent) -> None:
        """Render a generic UI notification event."""
        border_style = {
            UIEventLevel.INFO: "blue",
            UIEventLevel.SUCCESS: "green",
            UIEventLevel.WARNING: "yellow",
            UIEventLevel.ERROR: "red",
            UIEventLevel.DEBUG: "bright_black",
        }[event.level]

        if self._stream_has_output:
            self._finish_stream_line()

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
        if self._stream_has_output:
            self._finish_stream_line()
        elif response and render_response:
            self.render_markdown(response)

    def render_markdown(self, text: str) -> None:
        """Render agent output as plain text."""
        self.console.print(text, end="")


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