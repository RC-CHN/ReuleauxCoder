"""CLI rendering - event-driven UI renderer."""

from collections.abc import Callable

from rich.console import Console
from rich.markdown import Markdown

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
from reuleauxcoder.interfaces.cli.history import CLIHistoryPresenter
from reuleauxcoder.interfaces.cli.activity import CLIActivityPresenter
from reuleauxcoder.interfaces.cli.theme import CLITheme, DEFAULT_CLI_THEME
from reuleauxcoder.interfaces.cli.startup import show_banner as show_banner
from reuleauxcoder.interfaces.cli.streaming import (
    CLIStreamPresenter,
    find_committed_boundary,
)
from reuleauxcoder.interfaces.events import (
    ReasoningNoticePayload,
    InteractionPromptPayload,
    RemoteStreamPayload,
    RuntimeEventPayload,
    UIEvent,
    UIEventKind,
    ViewEventPayload,
)
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry
from reuleauxcoder.presentation import (
    DisplayTone,
    PresentationPolicy,
    PresentationReducer,
    ReasoningDisplay,
    Verbosity,
)
from reuleauxcoder.presentation.policy import fold_text
from reuleauxcoder.interfaces.cli.interaction_presenter import (
    render_interaction_request,
)

console = Console()
_find_committed_boundary = find_committed_boundary


class CLIRenderer:
    """Event-driven CLI renderer - subscribes to agent events."""

    def __init__(
        self,
        view_registry: ViewRendererRegistry | None = None,
        *,
        console_override: Console | None = None,
        reducer: PresentationReducer | None = None,
        policy: PresentationPolicy | None = None,
        theme: CLITheme = DEFAULT_CLI_THEME,
        terminal_width_provider: Callable[[], int | None] | None = None,
    ):
        self.console = console_override or console
        self.reducer = reducer or PresentationReducer(policy=policy)
        self.policy = self.reducer.policy
        self.theme = theme
        self.history = CLIHistoryPresenter(self.console, self.policy, theme)
        self.activity = CLIActivityPresenter(self.console, theme=theme)
        self.stream = CLIStreamPresenter(
            lambda text: self.render_content_markdown(text),
            lambda text: self.render_plain_text(text),
        )
        self.view_registry = view_registry or create_cli_view_registry()
        self._terminal_width_provider = terminal_width_provider
        # Reasoning streaming state
        self._reasoning_label_printed: bool = False

    def close(self) -> None:
        """Release terminal handlers/resources held by the renderer."""
        self.stream.reset()
        self.activity.stop()
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
            if not self.activity.is_active:
                cell = self.reducer.state.transcript.get(
                    f"tool:{payload.tool_call_id}"
                )
                tool_name = getattr(cell, "name", "tool")
                self.activity.start("TOOL", tool_name, timed=True)
            self.activity.push_output(payload.text, stream=payload.stream)
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
                self.history.notice(
                    f"LSP: {len(payload.diagnostics)} diagnostic(s) in "
                    f"{payload.file_path}",
                    level="warning",
                    category="lsp",
                )
        elif isinstance(payload, DiagnosticsCleared) and changes:
            if self.policy.verbosity is not Verbosity.COMPACT:
                self.history.notice(
                    f"clean {payload.file_path}", level="success", category="lsp"
                )
        elif isinstance(payload, ApprovalRequested) and changes:
            self.history.notice(
                payload.title, level="warning", category="approval"
            )
        elif isinstance(payload, ApprovalResolved) and changes:
            status = "approved" if payload.approved else "denied"
            self.history.notice(
                f"{status}: {payload.request_id}",
                level="success" if payload.approved else "warning",
                category="approval",
            )

    def _render_runtime_notification(self, payload: NotificationRaised) -> None:
        level = payload.severity.lower()
        if level in {"error", "warning"} or self.policy.verbosity is not Verbosity.COMPACT:
            self.history.notice(payload.message, level=level)

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
            self.activity.stop()
            self._close_active_content_block()
            render_interaction_request(
                self.console,
                event.payload.request,
                max_preview_lines=self.policy.tool_preview_lines,
                max_preview_chars=self.policy.tool_preview_chars,
                theme=self.theme,
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
        self.activity.stop()
        self.stream.append(token)

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
                if not self.activity.start(
                    "THINK", "processing", timed=False, retain=True
                ):
                    self.history.reasoning_indicator()
                self._reasoning_label_printed = True
            self.activity.bump()
            return

        # inline mode
        if not self._reasoning_label_printed:
            self.activity.stop()
            self._close_active_content_block()
            self.history.reasoning_prefix()
            self._reasoning_label_printed = True
        self.console.print(token, style=self.theme.style(DisplayTone.MUTED), end="")

    def _close_active_content_block(self) -> None:
        """Finalize the active content block before structured output."""
        self.stream.close()

    def _flush_remaining_content(self) -> None:
        """Compatibility hook for adapters finalizing a partial stream."""
        self.stream.flush_remaining()

    def _render_tool_start(self, name: str, args: dict | None) -> None:
        """Render tool call start."""
        self.activity.stop()
        self._close_active_content_block()
        self.history.tool_started(name, args)
        self.activity.start("TOOL", name, timed=True)

    def _render_tool_end(self, name: str, outcome: ToolOutcome) -> None:
        """Render tool call result."""
        self.activity.stop()
        self.history.tool_finished(name, outcome)

    def _render_subagent_completed(self, payload: SubagentFinished) -> None:
        """Render a concise sub-agent completion notification."""
        self.history.subagent_finished(payload)

    def _render_diff(self, result: str) -> None:
        """Render a diff with syntax highlighting."""
        render_diff_panel(
            result,
            self.console,
            theme=self.theme,
            max_lines=self.policy.tool_preview_lines,
            max_chars=self.policy.tool_preview_chars,
        )

    def _render_error(self, message: str | None) -> None:
        """Render an error message."""
        if message:
            self.activity.stop()
            self.history.notice(message, level="error")

    def _render_remote_stream(self, payload: RemoteStreamPayload) -> None:
        """Render raw remote stream chunk directly to terminal."""
        if not payload.chunk:
            return
        self.activity.stop()
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
            self.history.notice(
                message,
                level="info",
                category=event.payload.title,
            )
            return

        self._close_active_content_block()
        self.reducer.append_notice(
            notice_id=f"{event.timestamp}:{event.kind.value}:{event.message}",
            message=event.message,
            level=event.level.value,
            category=event.kind.value,
        )
        if not self.policy.should_render_notification(event.level.value):
            return
        self.history.notice(
            message,
            level=event.level.value,
            category=event.kind.value,
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
        self.activity.stop()
        self.stream.finalize(response, render_response=render_response)

    @property
    def _active_content_block(self):
        """Compatibility view for registered views and downstream adapters."""
        return self.stream.active_block

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


def show_error(text: str) -> None:
    CLIHistoryPresenter(console, PresentationPolicy()).notice(text, level="error")


def show_warning(text: str) -> None:
    CLIHistoryPresenter(console, PresentationPolicy()).notice(text, level="warning")


def show_info(text: str) -> None:
    CLIHistoryPresenter(console, PresentationPolicy()).notice(text, level="info")
