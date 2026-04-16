from unittest.mock import Mock

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry


def _renderer() -> CLIRenderer:
    return CLIRenderer(view_registry=ViewRendererRegistry([]))


def test_cli_renderer_skips_chat_end_render_when_response_already_streamed() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.stream_token("hello "))
    renderer.on_event(AgentEvent.stream_token("world"))
    renderer.on_event(AgentEvent.tool_call_start("shell", {"command": "pwd"}))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    renderer.render_markdown.assert_called_once_with("hello world")


def test_cli_renderer_renders_chat_end_when_requested() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.chat_end("final answer", render_response=True))

    renderer.render_markdown.assert_called_once_with("final answer")


def test_cli_renderer_finalizes_stream_without_rendering_chat_end_response() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.stream_token("hello\r\nworld\n"))
    renderer.on_event(AgentEvent.chat_end("hello\nworld", render_response=False))

    renderer.render_markdown.assert_called_once_with("hello\r\nworld\n")
