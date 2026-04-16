from unittest.mock import Mock

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry


def _renderer() -> CLIRenderer:
    return CLIRenderer(view_registry=ViewRendererRegistry([]))


def test_cli_renderer_streams_tokens_and_ends_line_before_tool_output() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.stream_token("hello "))
    renderer.on_event(AgentEvent.stream_token("world"))
    renderer.on_event(AgentEvent.tool_call_start("shell", {"command": "pwd"}))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    assert renderer.render_markdown.call_args_list == [(("hello ",),), (("world",),), (("\n",),)]


def test_cli_renderer_renders_chat_end_when_requested() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.chat_end("final answer", render_response=True))

    renderer.render_markdown.assert_called_once_with("final answer")


def test_cli_renderer_finalizes_stream_without_rendering_duplicate_chat_end_response() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.stream_token("hello world"))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    assert renderer.render_markdown.call_args_list == [(("hello world",),), (("\n",),)]


def test_cli_renderer_finalizes_stream_without_tail_patch() -> None:
    renderer = _renderer()
    renderer.render_markdown = Mock()

    renderer.on_event(AgentEvent.stream_token("hello"))
    renderer.on_event(AgentEvent.chat_end("hello world", render_response=False))

    assert renderer.render_markdown.call_args_list == [(("hello",),), (("\n",),)]
