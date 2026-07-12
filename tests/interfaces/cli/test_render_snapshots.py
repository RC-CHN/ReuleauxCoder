from __future__ import annotations

import pytest
from rich.console import Console

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.runtime.events import agent_event_to_runtime_event
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind
from reuleauxcoder.interfaces.view_registry import ViewRendererRegistry
from reuleauxcoder.presentation import (
    NotificationThreshold,
    PresentationPolicy,
    ToolOutputMode,
    Verbosity,
)


def render_agent_event(renderer: CLIRenderer, event: AgentEvent) -> None:
    renderer.on_runtime_event(agent_event_to_runtime_event(event))


@pytest.mark.parametrize(
    ("width", "expected"),
    [
        (
            80,
            "› shell(command='python -m pytest tests/unit --maxfail=1', …)\n"
            "  540 passed in 120.63s\n",
        ),
        (
            120,
            "› shell(command='python -m pytest tests/unit --maxfail=1', timeout=120, …)\n"
            "  540 passed in 120.63s\n",
        ),
    ],
)
def test_compact_tool_lifecycle_width_snapshot(width: int, expected: str) -> None:
    console = Console(record=True, width=width, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )
    arguments = {
        "command": "python -m pytest tests/unit --maxfail=1",
        "timeout": 120,
        "description": "run focused regression tests",
    }

    render_agent_event(
        renderer, AgentEvent.tool_call_start("shell", arguments, tool_call_id="call-1")
    )
    render_agent_event(
        renderer,
        AgentEvent.tool_call_end(
            "shell", "540 passed in 120.63s", tool_call_id="call-1"
        ),
    )

    assert console.export_text() == expected


def test_compact_notification_snapshot() -> None:
    console = Console(record=True, width=80, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )

    renderer.on_ui_event(UIEvent.info("Loaded session"))
    renderer.on_ui_event(UIEvent.success("Saved session"))
    renderer.on_ui_event(
        UIEvent.warning("Interrupted.", kind=UIEventKind.APPROVAL)
    )
    renderer.on_ui_event(UIEvent.debug("internal detail"))

    assert console.export_text() == (
        "Loaded session\n✓ Saved session\n⚠ approval: Interrupted.\n"
    )


@pytest.mark.parametrize(
    ("verbosity", "mode", "show_args", "width", "expected"),
    [
        (
            Verbosity.COMPACT,
            ToolOutputMode.SUMMARY,
            False,
            80,
            "› shell()\n  Command completed · line one\n",
        ),
        (
            Verbosity.COMPACT,
            ToolOutputMode.SUMMARY,
            False,
            120,
            "› shell()\n  Command completed · line one\n",
        ),
        (
            Verbosity.STANDARD,
            ToolOutputMode.PREVIEW,
            True,
            80,
            "› shell(command='python -m pytest tests/unit --maxfail=1', …)\n"
            "  line one\n… (output folded; 3 lines, 28 chars total; set "
            "ui.tool_output=full to show all)\nline three\n",
        ),
        (
            Verbosity.STANDARD,
            ToolOutputMode.PREVIEW,
            True,
            120,
            "› shell(command='python -m pytest tests/unit --maxfail=1', timeout=120, …)\n"
            "  line one\n… (output folded; 3 lines, 28 chars total; set "
            "ui.tool_output=full to show all)\nline three\n",
        ),
        (
            Verbosity.DEBUG,
            ToolOutputMode.FULL,
            True,
            80,
            "› shell(command='python -m pytest tests/unit --maxfail=1', …)\n"
            "  line one\nline two\nline three\n",
        ),
        (
            Verbosity.DEBUG,
            ToolOutputMode.FULL,
            True,
            120,
            "› shell(command='python -m pytest tests/unit --maxfail=1', timeout=120, …)\n"
            "  line one\nline two\nline three\n",
        ),
    ],
)
def test_verbosity_and_width_tool_snapshot(
    verbosity: Verbosity,
    mode: ToolOutputMode,
    show_args: bool,
    width: int,
    expected: str,
) -> None:
    console = Console(record=True, width=width, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]),
        console_override=console,
        policy=PresentationPolicy(
            verbosity=verbosity,
            tool_output_mode=mode,
            tool_preview_lines=2,
            tool_preview_chars=80,
            show_tool_args=show_args,
            notification_threshold=NotificationThreshold.DEBUG,
        ),
    )
    arguments = {
        "command": "python -m pytest tests/unit --maxfail=1",
        "timeout": 120,
        "description": "run focused regression tests",
    }
    outcome = ToolOutcome(
        summary="Command completed · line one",
        stdout="line one\nline two\nline three",
        exit_code=0,
    )

    render_agent_event(
        renderer,
        AgentEvent.tool_call_start("shell", arguments, tool_call_id="call-mode"),
    )
    render_agent_event(
        renderer,
        AgentEvent.tool_call_end(
            "shell",
            outcome.model_text,
            tool_call_id="call-mode",
            outcome=outcome,
        ),
    )

    assert console.export_text() == expected


def test_compact_tool_error_is_a_single_line_snapshot() -> None:
    console = Console(record=True, width=80, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )
    outcome = ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content="permission denied",
        error_kind=ToolErrorKind.DENIED,
    )

    render_agent_event(
        renderer,
        AgentEvent.tool_call_end(
            "shell", outcome.model_text, tool_call_id="failed", outcome=outcome
        ),
    )

    assert console.export_text() == "  × shell: permission denied\n"


def test_long_notification_is_head_tail_folded() -> None:
    console = Console(record=True, width=80, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]),
        console_override=console,
        policy=PresentationPolicy(tool_preview_lines=4, tool_preview_chars=80),
    )
    message = "\n".join(f"detail-{index}" for index in range(30))

    renderer.on_ui_event(UIEvent.warning(message))
    output = console.export_text()

    assert "detail-0" in output
    assert "detail-29" in output
    assert "output folded" in output
