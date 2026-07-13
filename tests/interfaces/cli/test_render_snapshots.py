from __future__ import annotations

import pytest
from rich.console import Console

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.runtime.events import agent_event_to_runtime_event
from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    AssistantContentDelta,
    RuntimeEvent,
    SubagentJobChanged,
)
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.interfaces.cli.render import CLIRenderer, show_banner
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
            " RUN  python -m pytest tests/unit --maxfail=1  timeout=120, …\n"
            " └ 540 passed in 120.63s\n",
        ),
        (
            120,
            " RUN  python -m pytest tests/unit --maxfail=1  timeout=120, "
            "description='run focused regression tests'\n"
            " └ 540 passed in 120.63s\n",
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
    renderer.on_ui_event(UIEvent.warning("Interrupted.", kind=UIEventKind.APPROVAL))
    renderer.on_ui_event(UIEvent.debug("internal detail"))

    assert console.export_text() == (
        " INFO  Loaded session\n OK  Saved session\n WARN  APPROVAL // Interrupted.\n"
    )


def test_append_only_renderer_hides_child_internals_but_keeps_approval() -> None:
    console = Console(record=True, width=80, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]),
        console_override=console,
        root_agent_id="root",
    )

    renderer.on_runtime_event(
        RuntimeEvent(
            payload=AssistantContentDelta("private child output"),
            agent_id="child-1",
        )
    )
    renderer.on_runtime_event(
        RuntimeEvent(
            payload=SubagentJobChanged(
                job_id="sj-1",
                mode="explore",
                task="private child task",
                status="completed",
            ),
            agent_id="child-1",
        )
    )
    renderer.on_runtime_event(
        RuntimeEvent(
            payload=ApprovalRequested(
                request_id="approval-child",
                title="Approve child shell",
            ),
            agent_id="child-1",
        )
    )

    rendered = console.export_text()
    assert "private child output" not in rendered
    assert "private child task" not in rendered
    assert "Approve child shell" in rendered


@pytest.mark.parametrize(
    ("verbosity", "mode", "show_args", "width", "expected"),
    [
        (
            Verbosity.COMPACT,
            ToolOutputMode.SUMMARY,
            False,
            80,
            " RUN \n └ Command completed · line one\n",
        ),
        (
            Verbosity.COMPACT,
            ToolOutputMode.SUMMARY,
            False,
            120,
            " RUN \n └ Command completed · line one\n",
        ),
        (
            Verbosity.STANDARD,
            ToolOutputMode.PREVIEW,
            True,
            80,
            " RUN  python -m pytest tests/unit --maxfail=1  timeout=120, …\n"
            " └ line one\n… (output folded; 3 lines, 28 chars total; set "
            "ui.tool_output=full to show all)\nline three\n",
        ),
        (
            Verbosity.STANDARD,
            ToolOutputMode.PREVIEW,
            True,
            120,
            " RUN  python -m pytest tests/unit --maxfail=1  timeout=120, "
            "description='run focused regression tests'\n"
            " └ line one\n… (output folded; 3 lines, 28 chars total; set "
            "ui.tool_output=full to show all)\nline three\n",
        ),
        (
            Verbosity.DEBUG,
            ToolOutputMode.FULL,
            True,
            80,
            " RUN  python -m pytest tests/unit --maxfail=1  timeout=120, …\n"
            " └ line one\nline two\nline three\n",
        ),
        (
            Verbosity.DEBUG,
            ToolOutputMode.FULL,
            True,
            120,
            " RUN  python -m pytest tests/unit --maxfail=1  timeout=120, "
            "description='run focused regression tests'\n"
            " └ line one\nline two\nline three\n",
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

    assert console.export_text() == " FAIL  shell  permission denied\n"


def test_unreviewed_diff_uses_shared_forge_review_frame() -> None:
    from reuleauxcoder.domain.agent.tool_outcome import ToolDiff

    console = Console(record=True, width=80, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )
    outcome = ToolOutcome(
        summary="Edited demo.txt",
        diff=ToolDiff(path="demo.txt", unified="--- a/demo\n+++ b/demo\n-old\n+new"),
        metadata={"operation": "edit", "show_diff_by_default": True},
    )

    render_agent_event(
        renderer,
        AgentEvent.tool_call_end(
            "edit_file", outcome.model_text, tool_call_id="edit", outcome=outcome
        ),
    )

    output = console.export_text()
    assert "EDIT RESULT" in output
    assert "APPLIED DIFF" in output
    assert set("┏┓┗┛┃━┌┐└┘│─").intersection(output)


def test_reviewed_diff_only_renders_completion_summary() -> None:
    from reuleauxcoder.domain.agent.tool_outcome import ToolDiff

    console = Console(record=True, width=80, color_system=None, force_terminal=False)
    renderer = CLIRenderer(
        view_registry=ViewRendererRegistry([]), console_override=console
    )
    outcome = ToolOutcome(
        summary="Edited demo.txt",
        diff=ToolDiff(path="demo.txt", unified="--- a/demo\n+++ b/demo\n-old\n+new"),
        metadata={
            "operation": "edit",
            "show_diff_by_default": True,
            "diff_reviewed": True,
        },
    )

    render_agent_event(
        renderer,
        AgentEvent.tool_call_end(
            "edit_file", outcome.model_text, tool_call_id="edit", outcome=outcome
        ),
    )

    assert console.export_text() == " └ Edited demo.txt\n"


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


@pytest.mark.parametrize("width", [40, 80, 120])
def test_startup_banner_is_compact_and_terminal_bounded(width: int) -> None:
    console = Console(record=True, width=width, color_system=None, force_terminal=False)

    show_banner(
        "demo/model",
        "https://example.invalid/openai-compatible/v1",
        "0.4.3",
        console_override=console,
        startup_events=(
            UIEvent.info("LSP: 9/9 language servers ready\n  ✓ python\n  ✓ rust"),
            UIEvent.success("Auto-resumed latest session: session_demo"),
        ),
    )
    output = console.export_text()

    assert "FORGE   REULEAUXCODER  //  V0.4.3" in output
    assert "SESSION PLATE" in output
    assert "demo/model" in output
    assert "LSP: 9/9 language servers" in output
    assert "ready" in output
    assert "Auto-resumed latest session" in output
    assert "/help" in output
    assert max(map(len, output.splitlines())) <= min(width, 88)
