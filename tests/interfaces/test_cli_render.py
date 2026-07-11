from unittest.mock import Mock

from rich.markdown import Markdown

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.views.common import render_markdown_panel
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry
from reuleauxcoder.presentation.models import AssistantCell, NoticeCell, ToolCell


def _renderer() -> CLIRenderer:
    return CLIRenderer(view_registry=ViewRendererRegistry([]))


def test_cli_renderer_buffers_until_tool_output_then_flushes_plain_text() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("hello "))
    renderer.on_event(AgentEvent.stream_token("world"))
    renderer.on_event(AgentEvent.tool_call_start("shell", {"command": "pwd"}))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    renderer.render_content_markdown.assert_called_once_with("hello world")
    renderer.render_plain_text.assert_called_once_with("\n")


def test_cli_renderer_renders_chat_end_when_requested() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()

    renderer.on_event(AgentEvent.chat_end("final answer", render_response=True))

    renderer.render_content_markdown.assert_called_once_with("final answer")


def test_cli_renderer_finalizes_stream_without_rendering_duplicate_chat_end_response() -> (
    None
):
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("hello world"))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    renderer.render_content_markdown.assert_called_once_with("hello world")
    renderer.render_plain_text.assert_called_once_with("\n")


def test_cli_renderer_finalizes_stream_without_tail_patch() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("hello"))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    renderer.render_content_markdown.assert_called_once_with("hello")
    renderer.render_plain_text.assert_called_once_with("\n")


def test_cli_renderer_tracks_completed_content_and_tool_blocks() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("hello"))
    renderer.on_event(
        AgentEvent.tool_call_start(
            "shell", {"command": "pwd"}, tool_call_id="call-1"
        )
    )
    renderer.on_event(
        AgentEvent.tool_call_end("shell", "ok", success=True, tool_call_id="call-1")
    )

    assert renderer._active_content_block is None
    assistant, tool = renderer.reducer.state.transcript.cells
    assert isinstance(assistant, AssistantCell)
    assert assistant.text == "hello"
    assert isinstance(tool, ToolCell)
    assert tool.arguments == {"command": "pwd"}
    assert tool.outcome is not None and tool.outcome.model_text == "ok"


def test_cli_renderer_tracks_notification_block_after_stream() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("hello"))
    renderer.on_ui_event(UIEvent.info("debug note", kind=UIEventKind.SYSTEM))

    assert renderer._active_content_block is None
    assistant, notice = renderer.reducer.state.transcript.cells
    assert isinstance(assistant, AssistantCell)
    assert assistant.text == "hello"
    assert isinstance(notice, NoticeCell)
    assert notice.message == "debug note"
    assert notice.category == UIEventKind.SYSTEM.value


def test_render_markdown_panel_closes_active_stream_block() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("hello"))
    rendered = render_markdown_panel(renderer, markdown_text="# Help", title="Help")

    assert rendered is True
    assert renderer._active_content_block is None
    assert renderer.reducer.state.transcript.cells[0].text == "hello"
    renderer.render_content_markdown.assert_called_once_with("hello")
    renderer.render_plain_text.assert_called_once_with("\n")


def test_cli_renderer_flushes_completed_paragraph_as_markdown() -> None:
    renderer = _renderer()
    renderer.render_content_markdown = Mock()
    renderer.render_plain_text = Mock()

    renderer.on_event(AgentEvent.stream_token("# Title\nline 1\n\nrest"))

    # With markdown-it block commitment, "# Title" (heading) and "line 1"
    # (paragraph) are two complete blocks; only "rest" stays pending.
    renderer.render_content_markdown.assert_called_once_with("# Title\nline 1\n")
    assert renderer._active_content_block is not None
    assert renderer._active_content_block.pending_text == "\nrest"
    renderer.render_plain_text.assert_not_called()


def test_cli_renderer_falls_back_to_plain_text_when_markdown_render_fails() -> None:
    renderer = _renderer()
    renderer.console = Mock()
    renderer.console.print.side_effect = [RuntimeError("boom"), None]

    renderer.render_content_markdown("**hi**")

    first_call, second_call = renderer.console.print.call_args_list
    assert isinstance(first_call.args[0], Markdown)
    assert first_call.kwargs == {"end": ""}
    assert second_call.args == ("**hi**",)
    assert second_call.kwargs == {"end": ""}
