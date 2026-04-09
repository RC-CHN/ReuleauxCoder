"""CLI rendering - event-driven UI renderer."""

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind, UIEventLevel

console = Console()


class CLIRenderer:
    """Event-driven CLI renderer - subscribes to agent events."""

    def __init__(self):
        self.console = console
        self._streamed_tokens: list[str] = []

    def on_event(self, event: AgentEvent) -> None:
        """Handle an agent event."""
        if event.event_type == AgentEventType.STREAM_TOKEN:
            self._render_token(event.data["token"])
        elif event.event_type == AgentEventType.TOOL_CALL_START:
            self._render_tool_start(event.tool_name, event.tool_args)
        elif event.event_type == AgentEventType.TOOL_CALL_END:
            self._render_tool_end(event.tool_name, event.tool_result)
        elif event.event_type == AgentEventType.CHAT_END:
            self.finalize_response(event.data.get("response", ""))
        elif event.event_type == AgentEventType.ERROR:
            self._render_error(event.error_message)

    def on_ui_event(self, event: UIEvent) -> None:
        """Handle a UI bus event."""
        if event.kind == UIEventKind.AGENT:
            agent_event = event.data.get("agent_event")
            if isinstance(agent_event, AgentEvent):
                self.on_event(agent_event)
            return

        self._render_notification(event)

    def _render_token(self, token: str) -> None:
        """Render a streaming token."""
        self._streamed_tokens.append(token)
        print(token, end="", flush=True)

    def _render_tool_start(self, name: str, args: dict | None) -> None:
        """Render tool call start."""
        # If we were streaming, add a newline before tool info
        if self._streamed_tokens:
            print()
            self._streamed_tokens.clear()
        args_str = brief(args) if args else ""
        self.console.print(f"[dim]> {name}({args_str})[/dim]")

    def _render_tool_end(self, name: str, result: str | None) -> None:
        """Render tool call result."""
        if not result:
            return
        # Special rendering for edit_file with diff
        if name == "edit_file" and "---" in result:
            self._render_diff(result)
        else:
            # Truncate long results
            display = result[:500] + "..." if len(result) > 500 else result
            self.console.print(f"[dim]{display}[/dim]")

    def _render_diff(self, result: str) -> None:
        """Render a diff with syntax highlighting."""
        render_diff_panel(result, self.console)

    def _render_error(self, message: str | None) -> None:
        """Render an error message."""
        if message:
            self.console.print(f"[red]{message}[/red]")

    def _render_notification(self, event: UIEvent) -> None:
        """Render a generic UI notification event."""
        style = {
            UIEventLevel.INFO: None,
            UIEventLevel.SUCCESS: "green",
            UIEventLevel.WARNING: "yellow",
            UIEventLevel.ERROR: "red",
            UIEventLevel.DEBUG: "dim",
        }[event.level]

        if self._streamed_tokens:
            print()
            self._streamed_tokens.clear()

        if style:
            self.console.print(f"[{style}]{event.message}[/{style}]")
        else:
            self.console.print(event.message)

    def finalize_response(self, response: str) -> None:
        """Finalize response rendering (for non-streamed or final output)."""
        if self._streamed_tokens:
            print()  # End the streamed line
            self._streamed_tokens.clear()
        elif response:
            self.render_markdown(response)

    def render_markdown(self, text: str) -> None:
        """Render markdown text."""
        self.console.print(Markdown(text))


def brief(kwargs: dict, maxlen: int = 80) -> str:
    """Brief representation of kwargs for display."""
    if not kwargs:
        return ""
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")


def show_banner(model: str, base_url: str | None, version: str) -> None:
    console.print(
        Panel(
            f"[bold]ReuleauxCoder[/bold] v{version}\n"
            f"Model: [cyan]{model}[/cyan]"
            + (f"  Base: [dim]{base_url}[/dim]" if base_url else "")
            + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]/quit[/bold] to exit.",
            border_style="blue",
        )
    )


def show_help() -> None:
    console.print(
        Panel(
            "[bold]Commands:[/bold]\n"
            "  /help             Show this help\n"
            "  /reset            Clear conversation history\n"
            "  /model <name>     Switch model mid-conversation\n"
            "  /tokens           Show token usage\n"
            "  /compact          Compress conversation context\n"
            "  /save             Save session to disk\n"
            "  /sessions         List saved sessions\n"
            "  /session <id>     Resume a saved session in current process\n"
            "  /session latest   Resume latest saved session\n"
            "  /approval show    Show approval rules\n"
            "  /approval set ... Update approval rules\n"
            "  /mcp show         Show MCP server status\n"
            "  /mcp enable <s>   Enable one MCP server\n"
            "  /mcp disable <s>  Disable one MCP server\n"
            "  /quit             Exit ReuleauxCoder",
            title="ReuleauxCoder Help",
            border_style="dim",
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